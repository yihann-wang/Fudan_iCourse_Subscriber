"""DashScope file ASR: private temporary upload, resumable tasks, real timing.

Models are passed through unchanged; compatibility is determined by the file API. Bearer credentials only
go to the configured API; OSS uploads use the short-lived policy and result
downloads use their signed URL. Neither URL nor policy is persisted or logged.
"""

import json
import math
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from ..artifacts import atomic_write_json, file_sha256
from .cloud import SpeechAPIError, parse_response


def _content(text):
    return re.sub(r"\W+", "", text, flags=re.UNICODE)


def _sentence_segments(sentence):
    """Split long sentences only when the service supplied matching word timing."""
    text = sentence["text"]
    whole = dict(start=float(sentence["begin_time"]) / 1000,
                 end=float(sentence["end_time"]) / 1000, text=text)
    words = sentence.get("words")
    if not words or (len(text) <= 36 and whole["end"] - whole["start"] <= 6):
        return [whole]
    timed = []
    previous = whole["start"]
    cursor = 0
    try:
        for word in words:
            value = word["text"]
            punctuation = word.get("punctuation") or ""
            if not isinstance(value, str) or not isinstance(punctuation, str):
                return [whole]
            if punctuation and not value.endswith(punctuation):
                value += punctuation
            position = text.find(value, cursor)
            if not value or position < 0 or text[cursor:position].strip():
                return [whole]
            end_cursor = position + len(value)
            value = text[cursor:position] + value  # Preserve spaces between English terms.
            cursor = end_cursor
            start, end = float(word["begin_time"]) / 1000, float(word["end_time"]) / 1000
            if (not all(map(math.isfinite, (start, end))) or start < previous or
                    end <= start or end > whole["end"] + .1):
                return [whole]
            previous = start
            timed.append(dict(start=start, end=end, text=value))
    except (KeyError, ValueError, TypeError):
        return [whole]
    if text[cursor:].strip():
        return [whole]
    if _content("".join(w["text"] for w in timed)) != _content(text):
        return [whole]
    result, group = [], None
    for word in timed:
        if group and (len(group["text"]) + len(word["text"]) > 36 or
                      word["end"] - group["start"] > 6):
            result.append(group)
            group = None
        if group is None:
            group = dict(word)
        else:
            group["text"] += word["text"]
            group["end"] = word["end"]
    if group:
        result.append(group)
    return result or [whole]


def parse_dashscope_result(value, duration):
    """Convert documented millisecond sentence/word times to our seconds schema."""
    try:
        tracks = value["transcripts"]
        source_ms = value["properties"]["original_duration_in_milliseconds"]
        if not isinstance(tracks, list) or len(tracks) > 1:
            raise ValueError
        text, segments = [], []
        for track in tracks:
            if track.get("channel_id", 0) != 0 or not isinstance(track["text"], str):
                raise ValueError
            sentences = track["sentences"]
            if not isinstance(sentences, list):
                raise ValueError
            sentence_text = []
            for sentence in sentences:
                if not isinstance(sentence["text"], str):
                    raise ValueError
                if sentence["text"].strip():
                    # Validate the sentence even if words would otherwise replace it.
                    raw = dict(start=float(sentence["begin_time"]) / 1000,
                               end=float(sentence["end_time"]) / 1000, text=sentence["text"])
                    parse_response(dict(text=raw["text"], segments=[raw]), duration)
                    segments.extend(_sentence_segments(sentence))
                    sentence_text.append(sentence["text"])
            if _content("".join(sentence_text)) != _content(track["text"]):
                raise ValueError
            text.append(track["text"])
        payload = dict(text="\n".join(text), segments=segments, duration=float(source_ms) / 1000)
        if payload["text"].strip() and not segments:
            raise ValueError
        return parse_response(payload, duration)
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise SpeechAPIError("阿里云返回的文字或时间戳不完整，未保存为完成结果。") from None


class _HTTPFailure(SpeechAPIError):
    def __init__(self, status):
        self.status = status
        detail = {400: "请核对模型和参数", 401: "API Key 无效或地域不匹配",
                  403: "无权访问，请核对模型开通状态、地域和余额",
                  404: "接口或云端任务不存在；旧任务可能已过期",
                  413: "音频过大，请缩短音频块", 429: "请求限流或额度不足"}
        super().__init__(f"阿里云语音服务 HTTP {status}：{detail.get(status, '服务暂时不可用')}。")


class DashScopeAPI:
    def __init__(self, settings, session=None):
        self.settings = settings.resolved()
        self.session = session or requests.Session()

    def close(self):
        self.session.close()

    def _json(self, method, url, *, authenticated=False, retry=False, progress=None, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            if not self.settings.api_key:
                raise SpeechAPIError("请填写阿里云百炼的语音 API Key。")
            headers["Authorization"] = "Bearer " + self.settings.api_key
        attempts = self.settings.retries + 1 if retry else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, url, headers=headers,
                    timeout=(15, min(60, self.settings.timeout_seconds)), allow_redirects=False, **kwargs)
            except requests.RequestException:
                error = SpeechAPIError("阿里云连接失败或超时；已提交的任务将在重试时继续查询。")
            else:
                try:
                    if response.status_code not in (200, 201, 202):
                        error = _HTTPFailure(response.status_code)
                        if response.status_code not in (408, 429, 500, 502, 503, 504):
                            raise error
                    else:
                        try:
                            data = response.json()
                        except ValueError:
                            raise SpeechAPIError("阿里云未返回有效 JSON。") from None
                        if not isinstance(data, dict):
                            raise SpeechAPIError("阿里云返回格式无效。")
                        return data
                finally:
                    response.close()
            if attempt + 1 == attempts:
                raise error
            if progress:
                progress(dict(event="retry", message="阿里云暂时不可用，正在重试查询"))
            time.sleep(min(2 ** (attempt + 1), 20))

    def _storage_url(self, value):
        url = urlsplit(value)
        base = urlsplit(self.settings.base_url)
        same_origin = url.scheme == base.scheme and url.netloc == base.netloc
        if (url.username or url.password or url.fragment or not url.hostname or
                not (same_origin or (url.scheme == "https" and url.hostname.endswith(".aliyuncs.com")))):
            raise SpeechAPIError("阿里云返回了非预期的文件地址，已停止传输。")
        return value

    def _policy(self):
        value = self._json("GET", self.settings.base_url + "/uploads", authenticated=True,
                           retry=True, params={"action": "getPolicy", "model": self.settings.model})
        try:
            policy = value["data"]
            for key in ("upload_host", "upload_dir", "oss_access_key_id", "policy", "signature",
                        "x_oss_object_acl", "x_oss_forbid_overwrite"):
                if not isinstance(policy[key], str) or not policy[key]:
                    raise ValueError
            if policy["x_oss_object_acl"] != "private":
                raise ValueError
            self._storage_url(policy["upload_host"])
            return policy
        except (KeyError, TypeError, ValueError):
            raise SpeechAPIError("阿里云未返回有效的私有上传凭证。") from None

    def check(self):
        self._policy()
        return dict(message="阿里云连接成功，已取得所选模型的上传凭证；未上传音频、未提交转录。实际识别权限以任务结果为准。")

    def _upload(self, path):
        policy = self._policy()
        if path.stat().st_size > float(policy.get("max_file_size_mb", self.settings.max_upload_mb)) * 1000000:
            raise SpeechAPIError("音频超过阿里云上传限制，请缩短音频块。")
        # Do not disclose course names in remote object paths.
        key = policy["upload_dir"].rstrip("/") + "/" + uuid4().hex + ".mp3"
        form = {"OSSAccessKeyId": policy["oss_access_key_id"], "policy": policy["policy"],
                "Signature": policy["signature"], "key": key, "x-oss-object-acl": "private",
                "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"], "success_action_status": "200"}
        try:
            with path.open("rb") as audio:
                response = self.session.post(policy["upload_host"], data=form,
                    files={"file": ("audio.mp3", audio, "audio/mpeg")},
                    timeout=(15, self.settings.timeout_seconds), allow_redirects=False)
        except requests.RequestException:
            raise SpeechAPIError("阿里云音频上传失败或超时，尚未提交转录任务，可重试。") from None
        try:
            if response.status_code not in (200, 201, 204):
                raise _HTTPFailure(response.status_code)
        finally:
            response.close()
        return "oss://" + key

    def transcribe(self, path, duration, progress=None, task_path=None):
        path = Path(path)
        task_path = Path(task_path) if task_path else None
        identity = dict(fingerprint=self.settings.fingerprint, audio_sha256=file_sha256(path), duration=duration)
        state = dict(identity)

        def save():
            if task_path:
                atomic_write_json(task_path, state, private=True)

        def notice(message):
            if progress:
                progress(dict(event="progress", message=message))

        if task_path and task_path.exists():
            try:
                state = json.loads(task_path.read_text())
                if not isinstance(state, dict) or any(state.get(k) != v for k, v in identity.items()):
                    raise ValueError
            except (ValueError, TypeError):
                raise SpeechAPIError("阿里云任务缓存不匹配或损坏，请清除此课转录缓存后重试。") from None
            if "result" in state:
                return parse_response(state["result"], duration)
            if not state.get("task_id"):
                raise SpeechAPIError("上次提交结果不确定，已停止自动重复提交。请在百炼核对任务，再清除此课转录缓存后重试。")
            notice("阿里云转录 · 恢复已提交任务，继续查询")
        else:
            notice("阿里云转录 · 正在上传音频")
            file_url = self._upload(path)
            parameters = {"channel_id": [0]}
            if self.settings.language:
                parameters["language_hints"] = [s for s in re.split(r"[,，\s]+", self.settings.language) if s]
            if self.settings.timestamp_alignment != "default":
                parameters["timestamp_alignment_enabled"] = self.settings.timestamp_alignment == "enabled"
            state["phase"] = "submitting"
            save()  # A lost response must not silently submit a second billable job.
            try:
                submitted = self._json("POST", self.settings.base_url + "/services/audio/asr/transcription",
                    authenticated=True, headers={"X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
                    json={"model": self.settings.model, "input": {"file_urls": [file_url]}, "parameters": parameters})
            except _HTTPFailure as exc:
                if exc.status in (400, 401, 403, 404, 413, 422, 429) and task_path:
                    task_path.unlink(missing_ok=True)
                raise
            try:
                task_id = submitted["output"]["task_id"]
                if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise SpeechAPIError("阿里云提交响应缺少任务编号；不会自动重复提交，请先核对云端任务。") from None
            state.update(task_id=task_id, phase="submitted")
            save()

        task_id = state["task_id"]
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
            raise SpeechAPIError("阿里云任务编号无效，请清除此课转录缓存。")
        deadline, previous_status = time.monotonic() + self.settings.timeout_seconds, None
        while True:
            result = self._json("GET", self.settings.base_url + "/tasks/" + task_id,
                                authenticated=True, retry=True, progress=progress)
            output = result.get("output", {})
            if not isinstance(output, dict):
                raise SpeechAPIError("阿里云任务响应格式无效，已保留任务编号。")
            status = output.get("task_status")
            if status == "SUCCEEDED":
                results = output.get("results")
                if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                    raise SpeechAPIError("阿里云任务没有返回唯一的音频结果。")
                if results[0].get("subtask_status") != "SUCCEEDED":
                    if task_path:
                        task_path.unlink(missing_ok=True)
                    raise SpeechAPIError("阿里云音频子任务失败，未保存为完成结果，请核对模型权限或音频格式。")
                url = self._storage_url(results[0].get("transcription_url", ""))
                notice("阿里云转录 · 正在获取文字和字幕时间戳")
                payload = parse_dashscope_result(self._json("GET", url, retry=True, progress=progress), duration)
                state["result"] = payload
                save()  # Keep until the parent has atomically stored its chunk checkpoint.
                return payload
            if status in ("FAILED", "CANCELED"):
                if task_path:
                    task_path.unlink(missing_ok=True)
                raise SpeechAPIError("阿里云转录任务失败或已取消，可重试；请核对模型权限、余额和音频格式。")
            if status not in ("PENDING", "RUNNING"):
                raise SpeechAPIError("阿里云任务状态无效或已过期，已保留任务编号；请核对云端任务后重试。")
            if status != previous_status:
                notice("阿里云转录 · " + ("正在排队" if status == "PENDING" else "正在识别并对齐字幕"))
                previous_status = status
            if time.monotonic() >= deadline:
                raise SpeechAPIError("阿里云转录等待超时，任务编号已保存；重试将继续查询，不会重新提交。")
            time.sleep(2)
