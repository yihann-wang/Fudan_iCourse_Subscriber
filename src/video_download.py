"""Bounded HTTP ranges with private, validator-bound download checkpoints.

Never join representations without a strong validator (RFC 9110, 13/14).
The final recording is replaced only after transport and media validation.
"""

import hashlib
import json
import os
import re
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .artifacts import atomic_write_json, internal_path

RANGE_BYTES = 32 * 1024 * 1024


class DownloadError(RuntimeError):
    """A safe, user-facing transport error; no URL or response body attached."""


def _paths(output):
    output = Path(output)
    return (internal_path(output.with_name(output.name + ".download.part")),
            internal_path(output.with_name(output.name + ".download.json")))


def _validator(headers):
    etag = headers.get("etag", "")
    if re.fullmatch(r'"[\x21\x23-\x7e]{0,510}"', etag):
        return ["etag", etag]
    # A weak ETag must not be used with If-Range, nor bypassed with a date.
    if etag:
        return None
    modified = headers.get("last-modified", "")
    try:
        age = parsedate_to_datetime(headers.get("date", "")) - parsedate_to_datetime(modified)
        if age.total_seconds() >= 60 and "\r" not in modified and "\n" not in modified:
            return ["last-modified", modified]
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _load(part, checkpoint, resource):
    try:
        saved = json.loads(checkpoint.read_text())
        validator = saved["validator"]
        total = saved["total"]
        if (saved.get("schema") == 1 and saved.get("resource") == resource
                and isinstance(total, int) and total > 0
                and 0 < part.stat().st_size <= total
                and isinstance(validator, list) and len(validator) == 2
                and validator[0] in {"etag", "last-modified"}
                and isinstance(validator[1], str) and len(validator[1]) <= 512
                and not any(c in validator[1] for c in "\r\n")):
            if validator[0] == "etag" and _validator({"etag": validator[1]}) != validator:
                return None
            if validator[0] == "last-modified":
                parsedate_to_datetime(validator[1])
            return saved
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        pass
    return None


def retained_bytes(output):
    """Report resumable progress without exposing private request metadata."""
    part, checkpoint = _paths(output)
    try:
        return part.stat().st_size if checkpoint.is_file() else 0
    except OSError:
        return 0


def download_video(client, video_url, output, *, chunk_size=256 * 1024,
                   progress=None, message=None, before_replace=None):
    """Download through the authenticated client, preserving safe partial data.

    ``progress`` receives (stored bytes, total bytes, newly received bytes).
    Signed query parameters can rotate; only a hash of origin/path is saved.
    A service without a strong validator falls back to a single full response.
    """
    from .media import CancelledError, probe

    output = Path(output)
    part, checkpoint = _paths(output)
    part.parent.mkdir(parents=True, exist_ok=True)
    url = urlsplit(video_url)
    resource = hashlib.sha256(f"{url.scheme}://{url.netloc}{url.path}".encode()).hexdigest()
    state = _load(part, checkpoint, resource)
    received = 0
    use_ranges = True

    def tell(text):
        if message:
            message(text)

    def reset():
        # Remove the binding before discarding bytes, even if interrupted here.
        checkpoint.unlink(missing_ok=True)
        part.unlink(missing_ok=True)

    if state:
        tell(f"继续下载 · 已保留 {part.stat().st_size / 1024**2:.1f} MB")
    else:
        reset()

    try:
        while True:
            offset = part.stat().st_size if state else 0
            if state and offset == state["total"]:
                break
            headers = {"Accept-Encoding": "identity"}
            requested_end = offset + RANGE_BYTES - 1
            if use_ranges:
                headers["Range"] = f"bytes={offset}-{requested_end}"
                if state:
                    headers["If-Range"] = state["validator"][1]
            response = client.get_video_response(video_url, headers=headers, timeout=(30, 90))
            try:
                status = response.status_code
                h = {k.lower(): v for k, v in response.headers.items()}
                if status == 416 and state:
                    size = re.fullmatch(r"bytes \*/(\d+)", h.get("content-range", ""))
                    if size and int(size[1]) != state["total"]:
                        reset()
                        state = None
                        raise DownloadError("服务器上的录像大小已变化，已清除旧断点，请重试。")
                if status not in (200, 206):
                    raise DownloadError(f"录像服务器返回 HTTP {status}；已保留可续传进度。")
                if "text/html" in h.get("content-type", "").lower():
                    raise DownloadError("下载返回登录页面，请重新登录。")
                if h.get("content-encoding", "identity").lower() != "identity":
                    raise DownloadError("录像服务器返回了压缩传输，无法安全续传。")
                length = h.get("content-length")
                if length is not None and not re.fullmatch(r"\d+", length):
                    raise DownloadError("录像服务器返回了无效的文件大小。")
                length = int(length) if length is not None else None
                validator = _validator(h)

                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", h.get("content-range", ""))
                    if not use_ranges or not match:
                        raise DownloadError("录像服务器返回的分段位置无效，未拼接文件。")
                    first, last, total = map(int, match.groups())
                    if (first != offset or last < first or last >= total or last > requested_end
                            or (length is not None and length != last - first + 1)):
                        raise DownloadError("录像服务器返回的分段位置或长度不匹配，未拼接文件。")
                    expected = last - first + 1
                    if state:
                        key, value = state["validator"]
                        # 206 after If-Range asserts a match; reject explicit conflicts.
                        if total != state["total"] or h.get(key, value) != value:
                            reset()
                            state = None
                            raise DownloadError("服务器上的录像已变化，已清除旧断点，下次将重新下载。")
                        validator = state["validator"]
                    elif not validator and last + 1 < total:
                        tell("服务器未提供安全续传标识，改用完整下载")
                        use_ranges = False
                        continue
                else:
                    if offset:
                        tell("服务器未接受断点或录像已变化，重新下载")
                    reset()
                    state = None
                    offset = 0
                    total = length or 0
                    expected = length

                if validator and total:
                    state = dict(schema=1, resource=resource, validator=validator, total=total)
                    # Bind before receiving any bytes. A crash leaves a known representation.
                    atomic_write_json(checkpoint, state, private=True)
                written = 0
                with part.open("ab" if offset else "wb") as stream:
                    os.chmod(part, 0o600)
                    try:
                        for data in response.iter_content(chunk_size=chunk_size):
                            if not data:
                                continue
                            if expected is not None and written + len(data) > expected:
                                state = None
                                raise DownloadError("录像分段超过声明长度，已丢弃异常断点。")
                            stream.write(data)
                            written += len(data)
                            received += len(data)
                            if progress:
                                progress(offset + written, total, received)
                    finally:
                        stream.flush()
                        os.fsync(stream.fileno())
                if expected is not None and written != expected:
                    raise DownloadError(
                        f"录像传输中断：本段收到 {written}/{expected} 字节；"
                        + ("已保留进度，可继续下载。" if state else "服务器不支持安全续传，需重新下载。"))
                if status == 200 or offset + written == total:
                    break
            finally:
                response.close()

        tell("正在检查录像完整性")
        try:
            probe(part)
        except (OSError, CancelledError):
            raise  # Missing tools, timeouts or unavailable storage can be retried locally.
        except Exception as exc:
            if isinstance(exc.__cause__, OSError):
                raise
            # The HTTP representation was complete but the media wasn't. Retrying
            # this exact checkpoint would only repeat the same validation failure.
            reset()
            state = None
            raise
        if before_replace:
            before_replace(output)
        os.replace(part, output)
        checkpoint.unlink(missing_ok=True)
        return output
    except requests.exceptions.Timeout:
        raise DownloadError("录像连接超时；重试时会检查并复用可续传进度。") from None
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
        raise DownloadError("录像连接中断；重试时会检查并复用可续传进度。") from None
    except requests.exceptions.RequestException:
        raise DownloadError("录像网络请求失败；重试时会检查并复用可续传进度。") from None
    finally:
        if state is None:
            # Without a validator, preserving partial bytes would imply unsafe reuse.
            reset()
