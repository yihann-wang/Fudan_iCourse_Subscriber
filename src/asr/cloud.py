"""OpenAI-compatible multipart audio transport; no local speech models."""

import math
import re
import time

import requests


class SpeechAPIError(RuntimeError):
    pass


def parse_response(value, duration, response_format=""):
    """Keep text independently of timing. Never invent timestamps for plain text."""
    segments = []
    language = ""
    if isinstance(value, str):
        if response_format == "srt":
            cues = re.findall(
                r"\d+\s*\n(\d\d):(\d\d):(\d\d)[,.](\d{3})\s*-->\s*"
                r"(\d\d):(\d\d):(\d\d)[,.](\d{3})[^\n]*\n(.*?)(?=\n\s*\n|\Z)",
                value.strip(), re.S,
            )
            if not cues:
                raise SpeechAPIError("语音服务未返回有效 SRT 字幕。")
            for *timing, text in cues:
                h, m, s, ms, eh, em, es, ems = map(int, timing)
                segments.append(dict(start=h*3600+m*60+s+ms/1000,
                                     end=eh*3600+em*60+es+ems/1000, text=text.strip()))
            text = "\n".join(s["text"] for s in segments)
        else:
            text = value.strip()
    elif isinstance(value, dict) and "error" not in value:
        text = value.get("text")
        segments = value.get("segments") or []
        if not segments and isinstance(value.get("words"), list):
            segments = [dict(start=w.get("start"), end=w.get("end"), text=w.get("word"))
                        for w in value["words"] if isinstance(w, dict)]
        language = value.get("language") or ""
        if text is None and isinstance(segments, list):
            text = " ".join(str(s.get("text", "")) for s in segments if isinstance(s, dict))
        reported = value.get("duration")
        if reported is not None:
            try:
                actual = float(reported)
                if not math.isfinite(actual) or actual < duration - max(2, duration * .01):
                    raise ValueError
            except (ValueError, TypeError):
                raise SpeechAPIError("服务返回的音频时长不完整，请缩短音频块或检查模型限制。") from None
    else:
        raise SpeechAPIError("语音服务返回格式不兼容；需要 text 或带时间戳的 segments。")
    if not isinstance(text, str):
        raise SpeechAPIError("语音服务未返回文本字段。")
    if not isinstance(segments, list) or not isinstance(language, str):
        raise SpeechAPIError("语音服务返回的字幕格式无效。")
    normalized = []
    previous = 0.0
    for segment in segments:
        try:
            start, end = float(segment["start"]), float(segment["end"])
            words = segment["text"]
            if (not isinstance(words, str) or not all(map(math.isfinite, (start, end))) or
                    start < 0 or end <= start or start < previous or end > duration + 2):
                raise ValueError
        except (TypeError, ValueError, KeyError):
            raise SpeechAPIError("服务返回了无效时间戳；可改用 JSON 文本格式。") from None
        previous = start
        if words.strip() and start < duration:
            normalized.append(dict(start=start, end=min(end, duration), text=words.strip()))
    return dict(text=text.strip(), segments=normalized, language=language)


class CloudAPI:
    def __init__(self, settings, session=None):
        self.settings = settings.resolved()
        self.session = session or requests.Session()

    def close(self):
        self.session.close()

    def _request(self, method, route, *, path=None, progress=None):
        settings = self.settings
        if not settings.api_key:
            raise SpeechAPIError("请在设置中填写语音服务 API Key；本地转录已移除。")
        data = {"model": settings.model}
        for key, val in (("language", settings.language), ("prompt", settings.initial_prompt),
                         ("response_format", settings.response_format)):
            if val:
                data[key] = val
        for attempt in range(settings.retries + 1):
            response = None
            try:
                kwargs = dict(headers={"Authorization": "Bearer " + settings.api_key},
                              timeout=(15, settings.timeout_seconds), allow_redirects=False)
                if path is not None:
                    with open(path, "rb") as audio:
                        response = self.session.request(method, settings.base_url + route,
                            data=data, files={"file": ("audio.mp3", audio, "audio/mpeg")}, **kwargs)
                else:
                    response = self.session.request(method, settings.base_url + route, **kwargs)
            except requests.RequestException:
                # Do not include exception strings: they can contain credentials or URLs.
                message = "语音请求连接失败或超时。已完成的音频块会保留。"
            else:
                if response.status_code == 200:
                    return response
                status = response.status_code
                details = {
                    401: "API Key 无效或已过期", 403: "无权使用该模型或服务",
                    404: "请检查服务地址和模型名称", 413: "请减小音频块时长或上传大小",
                    400: "请检查模型名称、返回格式和可选参数", 422: "模型不接受当前音频或参数",
                    429: "请求频率或额度达到上限",
                }
                message = f"语音服务 HTTP {status}：{details.get(status, '服务暂时不可用')}。"
                if status != 429 and status not in {408, 500, 502, 503, 504}:
                    response.close()
                    raise SpeechAPIError(message)
            delay = min(2 ** (attempt + 1), 30)
            if response is not None:
                try:
                    delay = min(60, max(delay, float(response.headers.get("Retry-After", 0))))
                except ValueError:
                    pass
                response.close()
            if attempt == settings.retries:
                raise SpeechAPIError(message)
            if progress:
                progress(dict(event="retry", message=f"{message} {delay:g} 秒后重试（{attempt+1}/{settings.retries}）"))
            time.sleep(delay)

    def transcribe(self, path, duration, progress=None):
        response = self._request("POST", "/audio/transcriptions", path=path, progress=progress)
        try:
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise SpeechAPIError("语音地址返回了网页，请填写 API 地址。")
            if self.settings.response_format in {"text", "srt"}:
                # Some compatible providers ignore the requested format and return JSON.
                try:
                    value = response.json()
                except ValueError:
                    value = response.text
            else:
                try:
                    value = response.json()
                except ValueError:
                    raise SpeechAPIError("语音服务未返回有效 JSON，请检查返回格式设置。") from None
            return parse_response(value, duration, self.settings.response_format)
        finally:
            response.close()

    def check(self):
        response = self._request("GET", "/models")
        try:
            try:
                models = response.json()["data"]
                available = any(m.get("id") == self.settings.model for m in models)
            except (ValueError, KeyError, TypeError, AttributeError):
                raise SpeechAPIError("服务未提供兼容的模型列表；请查阅服务商文档。") from None
            if not available:
                raise SpeechAPIError("连接成功，但模型列表中未找到所填模型；请核对完整模型名称。")
            return dict(message="连接成功，模型可见；未上传音频。实际转录能力以任务结果为准。")
        finally:
            response.close()
