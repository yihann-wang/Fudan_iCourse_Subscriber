"""DashScope file ASR: private temporary upload, resumable tasks, real timing.

Models are passed through unchanged; compatibility is determined by the file API. Bearer credentials only
go to the configured API; OSS uploads use the short-lived policy and result
downloads use their signed URL. Neither URL nor policy is persisted or logged.
"""

import errno
import json
import math
import re
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from ..artifacts import atomic_write_json, file_sha256
from .cloud import SpeechAPIError, parse_response


# Documented file-ASR outcomes, not model names or heuristic message matching.
_EMPTY_RESULT_CODES = frozenset({"ASR_RESPONSE_HAVE_NO_WORDS", "SUCCESS_WITH_NO_VALID_FRAGMENT"})
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_SSL_ERROR_NAMES = {value: name for name, value in vars(ssl).items()
                    if name.startswith("SSL_ERROR_") and isinstance(value, int)}
_DNS_ERROR_NAMES = {value: name for name, value in vars(socket).items()
                    if name.startswith("EAI_") and isinstance(value, int)}


def retry_delay(attempt):
    """Back off safe reads and pre-submission uploads, never ASR submission."""
    return min(30 * 2 ** attempt, 60)


def worker_timeout(settings):
    """Budget policy, upload, submit, polling and result retrieval separately."""
    request = 15 + min(60, settings.timeout_seconds)
    attempts = settings.retries + 1
    backoff = sum(retry_delay(i) for i in range(settings.retries))
    read_budget = request * attempts + backoff
    # Each upload attempt obtains a fresh policy, whose GET may itself retry.
    upload_budget = attempts * (read_budget + 15 + settings.timeout_seconds) + backoff
    return upload_budget + request + settings.timeout_seconds + 2 * read_budget + 60


def _recovery_hint(phase):
    if phase in {"获取上传凭证", "上传音频"}:
        return "尚未提交识别任务；可重试，已完成块会复用。"
    if phase == "提交识别任务":
        return "提交结果不确定，不会自动重复提交；请先核对云端任务。"
    if phase in {"查询任务状态", "取回识别结果"}:
        return "已保留任务编号；重试继续获取原任务结果，不会重新提交。"
    return "请稍后重试。"


def _network_failure(exc, phase):
    # Never stringify transport exceptions: they can contain signed URLs,
    # proxy passwords or headers. Keep only known types and OS error names.
    pending, seen, codes, dns = [exc], set(), [], False
    while pending and len(seen) < 20:
        item = pending.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        dns = dns or isinstance(item, socket.gaierror)
        # SSL and resolver errors have their own numeric namespaces. In
        # particular SSL_ERROR_EOF == 8 must never be displayed as ENOEXEC.
        names = (_SSL_ERROR_NAMES if isinstance(item, ssl.SSLError) else
                 _DNS_ERROR_NAMES if isinstance(item, socket.gaierror) else errno.errorcode)
        code = names.get(getattr(item, "errno", None))
        if code and code not in codes:
            codes.append(code)
        pending.extend(value for value in (
            getattr(item, "__cause__", None), getattr(item, "__context__", None),
            getattr(item, "reason", None), *getattr(item, "args", ()),
        ) if isinstance(value, BaseException))
    kinds = ((requests.exceptions.ProxyError, "代理连接失败", "ProxyError"),
             (requests.exceptions.SSLError, "TLS 安全连接失败", "SSLError"),
             (requests.exceptions.ConnectTimeout, "建立连接超时", "ConnectTimeout"),
             (requests.exceptions.ReadTimeout, "读取响应超时", "ReadTimeout"),
             (requests.exceptions.Timeout, "请求超时", "Timeout"),
             (requests.exceptions.ConnectionError, "连接失败或中断", "ConnectionError"),
             (requests.exceptions.ChunkedEncodingError, "响应传输中断", "ChunkedEncodingError"))
    label, kind = next(((label, kind) for cls, label, kind in kinds if isinstance(exc, cls)),
                       ("网络请求失败", "RequestException"))
    if dns:
        label = "域名解析失败"
    detail = "，".join([kind, *codes[:2]])
    return SpeechAPIError(f"阿里云{phase}失败：{label}（{detail}）。{_recovery_hint(phase)}")


def _is_empty_recognition(output):
    """Only normalize explicit no-recognition outcomes for our single file."""
    if output.get("task_status") not in ("FAILED", "SUCCEEDED"):
        return False
    sources = [output]
    results = output.get("results")
    if results is not None:
        if (not isinstance(results, list) or len(results) != 1 or
                not isinstance(results[0], dict) or results[0].get("subtask_status") != "FAILED"):
            return False
        sources.append(results[0])
    elif output.get("task_status") != "FAILED":
        return False
    codes = [source["code"] for source in sources if source.get("code") not in (None, "")]
    return bool(codes) and all(isinstance(code, str) and code in _EMPTY_RESULT_CODES for code in codes)


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
    def __init__(self, status, phase="请求"):
        self.status = status
        self.phase = phase
        detail = {400: "请核对模型和参数", 401: "API Key 无效或地域不匹配",
                  403: "无权访问，请核对模型开通状态、地域和余额",
                  404: "接口或云端任务不存在；旧任务可能已过期",
                  413: "音频过大，请缩短音频块", 429: "请求限流或额度不足"}
        super().__init__(f"阿里云{phase} HTTP {status}：{detail.get(status, '服务暂时不可用')}。")


class DashScopeAPI:
    def __init__(self, settings, session=None):
        self.settings = settings.resolved()
        self.session = session or requests.Session()

    def close(self):
        self.session.close()

    def _task_failure(self, output, state, task_path, *, subtask=False):
        # Service messages can contain signed URLs, credentials or source text.
        # Keep only bounded error codes and the already validated task identity.
        details = []
        results = output.get("results")
        sources = [item for item in results if isinstance(item, dict)
                   and item.get("subtask_status") == "FAILED"] if isinstance(results, list) else []
        for source in [output, *sources]:
            code = source.get("code")
            if (isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", code)
                    and not code.lower().startswith("sk-")
                    and (not self.settings.api_key or self.settings.api_key not in code)
                    and code not in details):
                details.append(code)
        details = details[:4]
        if task_path:
            atomic_write_json(task_path.with_suffix(".failure.json"), {
                **{key: state[key] for key in ("fingerprint", "audio_sha256", "duration", "task_id")},
                "phase": "failed", "error_codes": details,
                "task_status": "CANCELED" if output.get("task_status") == "CANCELED" else "FAILED",
            }, private=True)
            task_path.unlink(missing_ok=True)
        label = "阿里云音频子任务失败" if subtask else "阿里云转录任务失败或已取消"
        reason = "、".join(details) if details else "服务未提供可安全显示的错误码"
        saved = "失败诊断已保留；" if task_path else ""
        raise SpeechAPIError(f"{label}（{reason}）。{saved}可据错误码核对后重试，已完成块无需重做。")

    def _json(self, method, url, *, authenticated=False, retry=False, progress=None, phase="请求", **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            if not self.settings.api_key:
                raise SpeechAPIError("请填写阿里云百炼的语音 API Key。")
            headers["Authorization"] = "Bearer " + self.settings.api_key
        attempts = self.settings.retries + 1 if retry and method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, url, headers=headers,
                    timeout=(15, min(60, self.settings.timeout_seconds)), allow_redirects=False, **kwargs)
            except requests.RequestException as exc:
                error = _network_failure(exc, phase)
            else:
                try:
                    if response.status_code not in (200, 201, 202):
                        error = _HTTPFailure(response.status_code, phase)
                        if response.status_code not in _RETRYABLE_HTTP_STATUSES:
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
                raise error from None
            delay = retry_delay(attempt)
            if progress:
                progress(dict(event="retry", message=f"{error} {delay} 秒后重试{phase}"
                              f"（{attempt + 1}/{self.settings.retries}）"))
            time.sleep(delay)

    def _storage_url(self, value):
        url = urlsplit(value)
        base = urlsplit(self.settings.base_url)
        same_origin = url.scheme == base.scheme and url.netloc == base.netloc
        if (url.username or url.password or url.fragment or not url.hostname or
                not (same_origin or (url.scheme == "https" and url.hostname.endswith(".aliyuncs.com")))):
            raise SpeechAPIError("阿里云返回了非预期的文件地址，已停止传输。")
        return value

    def _policy(self, progress=None):
        value = self._json("GET", self.settings.base_url + "/uploads", authenticated=True,
                           retry=True, progress=progress, phase="获取上传凭证",
                           params={"action": "getPolicy", "model": self.settings.model})
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

    def _upload(self, path, progress=None):
        # Uploads only create temporary objects: no billable ASR task exists
        # yet. A lost response may leave an unused object until its expiry.
        # Use a fresh policy/key/file handle, never reuse the uncertain object
        # or retry transcribe() as a whole (which could duplicate submission).
        attempts = self.settings.retries + 1
        for attempt in range(attempts):
            try:
                return self._upload_once(path, progress)
            except _HTTPFailure as exc:
                if exc.phase != "上传音频" or exc.status not in _RETRYABLE_HTTP_STATUSES:
                    raise
                error = exc
            except requests.RequestException as exc:
                error = _network_failure(exc, "上传音频")
                if not isinstance(exc, (requests.ConnectionError, requests.Timeout,
                                       requests.exceptions.ChunkedEncodingError)):
                    raise error from None
            if attempt + 1 == attempts:
                raise error from None
            self.session.close()  # Discard broken pooled proxy/TLS connections.
            delay = retry_delay(attempt)
            if progress:
                progress(dict(event="retry", message=f"{error} {delay} 秒后重试上传音频"
                              f"（{attempt + 1}/{self.settings.retries}）"))
            time.sleep(delay)

    def _upload_once(self, path, progress=None):
        if progress:
            progress(dict(event="progress", message="阿里云转录 · 正在获取上传凭证"))
        policy = self._policy(progress)
        if path.stat().st_size > float(policy.get("max_file_size_mb", self.settings.max_upload_mb)) * 1000000:
            raise SpeechAPIError("音频超过阿里云上传限制，请缩短音频块。")
        # Do not disclose course names in remote object paths.
        key = policy["upload_dir"].rstrip("/") + "/" + uuid4().hex + ".mp3"
        form = {"OSSAccessKeyId": policy["oss_access_key_id"], "policy": policy["policy"],
                "Signature": policy["signature"], "key": key, "x-oss-object-acl": "private",
                "x-oss-forbid-overwrite": policy["x_oss_forbid_overwrite"], "success_action_status": "200"}
        if progress:
            progress(dict(event="progress", message="阿里云转录 · 正在上传音频"))
        with path.open("rb") as audio:
            response = self.session.post(policy["upload_host"], data=form,
                files={"file": ("audio.mp3", audio, "audio/mpeg")},
                timeout=(15, self.settings.timeout_seconds), allow_redirects=False)
        try:
            if response.status_code not in (200, 201, 204):
                raise _HTTPFailure(response.status_code, "上传音频")
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

        def finish(payload):
            state["result"] = payload
            save()  # Survives interruption before the parent saves its chunk.
            if task_path:
                task_path.with_suffix(".failure.json").unlink(missing_ok=True)
            return payload

        # Version 0.6.4 retained a confirmed no-words outcome as a failure.
        # Reuse it only for exactly the same encoded audio and ASR settings.
        if task_path and not task_path.exists():
            failure_path = task_path.with_suffix(".failure.json")
            try:
                failed = json.loads(failure_path.read_text())
            except (OSError, ValueError):
                failed = None
            if isinstance(failed, dict):
                codes = failed.get("error_codes")
                if (all(failed.get(k) == v for k, v in identity.items())
                        and failed.get("phase") == "failed" and failed.get("task_status") == "FAILED"
                        and isinstance(failed.get("task_id"), str)
                        and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", failed["task_id"])
                        and isinstance(codes, list) and codes
                        and all(isinstance(c, str) and c in _EMPTY_RESULT_CODES for c in codes)):
                    state.update(task_id=failed["task_id"], phase="no_words")
                    notice("阿里云转录 · 复用已确认的空识别结果，将标记时间段并继续")
                    return finish(dict(text="", segments=[], language=""))

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
            file_url = self._upload(path, progress)
            parameters = {"channel_id": [0]}
            if self.settings.language:
                parameters["language_hints"] = [s for s in re.split(r"[,，\s]+", self.settings.language) if s]
            if self.settings.timestamp_alignment != "default":
                parameters["timestamp_alignment_enabled"] = self.settings.timestamp_alignment == "enabled"
            state["phase"] = "submitting"
            save()  # A lost response must not silently submit a second billable job.
            notice("阿里云转录 · 正在提交识别任务")
            try:
                submitted = self._json("POST", self.settings.base_url + "/services/audio/asr/transcription",
                    phase="提交识别任务",
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
                                authenticated=True, retry=True, progress=progress, phase="查询任务状态")
            output = result.get("output", {})
            if not isinstance(output, dict):
                raise SpeechAPIError("阿里云任务响应格式无效，已保留任务编号。")
            status = output.get("task_status")
            if _is_empty_recognition(output):
                state["phase"] = "no_words"
                notice("阿里云转录 · 本块未识别到文字，将标记时间段并继续")
                return finish(dict(text="", segments=[], language=""))
            if status == "SUCCEEDED":
                results = output.get("results")
                if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                    raise SpeechAPIError("阿里云任务没有返回唯一的音频结果。")
                if results[0].get("subtask_status") != "SUCCEEDED":
                    self._task_failure(output, state, task_path, subtask=True)
                url = self._storage_url(results[0].get("transcription_url", ""))
                notice("阿里云转录 · 正在获取文字和字幕时间戳")
                payload = parse_dashscope_result(self._json("GET", url, retry=True, progress=progress,
                                                           phase="取回识别结果"), duration)
                return finish(payload)
            if status in ("FAILED", "CANCELED"):
                self._task_failure(output, state, task_path)
            if status not in ("PENDING", "RUNNING"):
                raise SpeechAPIError("阿里云任务状态无效或已过期，已保留任务编号；请核对云端任务后重试。")
            if status != previous_status:
                notice("阿里云转录 · " + ("正在排队" if status == "PENDING" else "正在识别并对齐字幕"))
                previous_status = status
            if time.monotonic() >= deadline:
                raise SpeechAPIError("阿里云转录等待超时，任务编号已保存；重试将继续查询，不会重新提交。")
            time.sleep(2)
