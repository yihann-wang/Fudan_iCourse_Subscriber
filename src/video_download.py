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
from .video_storage import (
    SEGMENT_BYTES,
    StorageIntegrityError,
    checked_write_json,
    checkpoint_path,
    flush_file,
    readback,
    storage_error,
)

RANGE_BYTES = 32 * 1024 * 1024


class DownloadError(RuntimeError):
    """A safe, user-facing transport error; no URL or response body attached."""


class DownloadedMediaError(DownloadError):
    """Transport/readback succeeded, but media validation failed; retain evidence."""


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
    if not checkpoint.exists():
        return None
    try:
        if checkpoint.is_symlink() or checkpoint.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("invalid receipt")
        saved = json.loads(checkpoint.read_text())
        if saved.get("schema") != 2 or saved.get("resource") != resource:
            return None
        validator, total, segments = saved["validator"], saved["total"], saved["segments"]
        if type(total) is not int or total < 0 or not isinstance(segments, list):
            raise ValueError("invalid receipt")
        if type(saved.get("complete")) is not bool or saved.get("status") not in {"receiving", "storage_failed", "media_failed"}:
            raise ValueError("invalid receipt status")
        if validator is not None:
            if (not isinstance(validator, list) or len(validator) != 2
                    or validator[0] not in {"etag", "last-modified"}
                    or not isinstance(validator[1], str) or len(validator[1]) > 512
                    or any(c in validator[1] for c in "\r\n")):
                raise ValueError("invalid validator")
            if validator[0] == "etag" and _validator({"etag": validator[1]}) != validator:
                raise ValueError("invalid validator")
            if validator[0] == "last-modified":
                parsedate_to_datetime(validator[1])
        end = 0
        for offset, size, digest in segments:
            if (type(offset) is not int or offset != end or type(size) is not int
                    or not 0 < size <= SEGMENT_BYTES or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise ValueError("invalid segment")
            end += size
        if (total and end > total) or (saved.get("complete") and end != total):
            raise ValueError("invalid length")
        if saved.get("status") == "storage_failed":
            raise StorageIntegrityError("该录像有写入或回读失败记录；请检查保存位置后使用“重新下载指定录像”。")
        if saved.get("status") == "media_failed":
            return None  # A new, user-started run can request a fresh representation.
        if not validator and not saved.get("complete"):
            return None  # An incomplete unbound representation cannot be resumed.
        if end and (not part.is_file() or part.stat().st_size < end):
            raise StorageIntegrityError("临时录像缺少已记录的数据；请检查保存位置后重新下载指定录像。")
        return saved
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise StorageIntegrityError("本机下载摘要无法读取；已保留临时录像，请重新下载指定录像。") from exc


def retained_bytes(output):
    """Report resumable progress without exposing private request metadata."""
    part, checkpoint = _paths(output)
    try:
        saved = json.loads(checkpoint_path(output).read_text())
        if saved.get("status") not in {"storage_failed", "media_failed"} and (saved.get("validator") or saved.get("complete")):
            return min(part.stat().st_size, sum(s[1] for s in saved["segments"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 0
    return 0


def download_video(client, video_url, output, *, chunk_size=256 * 1024,
                   progress=None, message=None, before_replace=None, on_verified=None,
                   verify_progress=None, restart=False):
    """Download through the authenticated client, preserving safe partial data.

    ``progress`` receives (stored bytes, total bytes, newly received bytes).
    Signed query parameters can rotate; only a hash of origin/path is saved.
    A service without a strong validator falls back to a single full response.
    """
    from .media import CancelledError, probe

    output = Path(output)
    part, checkpoint = _paths(output)
    receipt = checkpoint_path(output)
    # Never silently create a directory on the system disk after a volume vanishes.
    absolute = output.absolute()
    if len(absolute.parts) > 2 and absolute.parts[1] == "Volumes":
        if not os.path.ismount(Path(*absolute.parts[:3])):
            raise StorageIntegrityError("外置保存卷未挂载；未创建下载文件，请重新连接磁盘。")
    if any(p.is_symlink() for p in (output, part, checkpoint, part.parent)):
        raise StorageIntegrityError("下载目标或临时目录含符号链接，未写入。")
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        device = part.parent.stat().st_dev
    except OSError as exc:
        raise storage_error("访问", exc) from exc
    url = urlsplit(video_url)
    resource = hashlib.sha256(f"{url.scheme}://{url.netloc}{url.path}".encode()).hexdigest()
    state = None
    received = 0
    use_ranges = True
    checking_media = False

    def tell(text):
        if message:
            message(text)

    def reset():
        # Remove the binding before discarding bytes, even if interrupted here.
        checkpoint.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)

    def persist():
        atomic_write_json(receipt, state, private=True)
        checked_write_json(checkpoint, state)

    def check_device():
        if part.parent.stat().st_dev != device:
            raise StorageIntegrityError("下载期间保存卷发生变化；已停止写入，请重新连接并检查磁盘。")

    try:
        if restart:
            reset()
        state = _load(part, receipt, resource)
        if state:
            tell("正在回读校验续传数据")
            if state["segments"]:
                readback(part, state["segments"], progress=verify_progress)
            committed = sum(s[1] for s in state["segments"])
            if state["total"] and committed == state["total"]:
                state["complete"] = True
            if part.exists() and part.stat().st_size > committed:
                # A crash may leave a tail whose network hash was never committed.
                with part.open("r+b") as stream:
                    stream.truncate(committed)
                    flush_file(stream)
            tell(f"继续下载 · 已校验 {committed / 1024**2:.1f} MB")
        else:
            if part.exists():
                tell("旧断点缺少可信接收摘要或媒体检查失败，本次重新下载")
            reset()
        while True:
            check_device()
            offset = sum(s[1] for s in state["segments"]) if state else 0
            if state and state.get("complete"):
                break
            headers = {"Accept-Encoding": "identity"}
            requested_end = offset + RANGE_BYTES - 1
            if use_ranges:
                headers["Range"] = f"bytes={offset}-{requested_end}"
                if state and state["validator"]:
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
                    if state and state["validator"]:
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

                if state is None:
                    state = dict(schema=2, resource=resource, validator=validator if total else None,
                                 total=total, segments=[], complete=False, status="receiving")
                # The system-disk receipt is the authoritative network-byte baseline.
                persist()
                written = 0
                segment_size = 0
                segment_hash = hashlib.sha256()
                with part.open("ab" if offset else "wb") as stream:
                    os.chmod(part, 0o600)

                    def commit_segment():
                        nonlocal segment_size, segment_hash
                        if not segment_size:
                            return
                        check_device()
                        flush_file(stream)
                        segment = [offset + written - segment_size, segment_size, segment_hash.hexdigest()]
                        state["segments"].append(segment)
                        segment_size = 0
                        segment_hash = hashlib.sha256()
                        persist()
                        readback(part, [segment])

                    try:
                        for data in response.iter_content(chunk_size=chunk_size):
                            if not data:
                                continue
                            if expected is not None and written + len(data) > expected:
                                raise DownloadError("录像分段超过声明长度，未写入超出的数据。")
                            view = memoryview(data)
                            while view:
                                chunk = view[:SEGMENT_BYTES - segment_size]
                                if stream.write(chunk) != len(chunk):
                                    raise StorageIntegrityError("保存位置未完整写入收到的数据；已停止下载。")
                                segment_hash.update(chunk)
                                segment_size += len(chunk)
                                written += len(chunk)
                                received += len(chunk)
                                view = view[len(chunk):]
                                if segment_size == SEGMENT_BYTES:
                                    commit_segment()
                            if progress:
                                progress(offset + written, total, received)
                    finally:
                        commit_segment()
                if expected is not None and written != expected:
                    raise DownloadError(
                        f"录像传输中断：本段收到 {written}/{expected} 字节；"
                        + ("已保留进度，可继续下载。" if state["validator"] else "服务器不支持安全续传，需重新下载。"))
                if status == 200 or offset + written == total:
                    state.update(complete=True, total=offset + written)
                    persist()
                    break
            finally:
                response.close()

        tell("正在回读校验完整录像")
        digest = readback(part, state["segments"], expected_size=state["total"], progress=verify_progress)
        tell("正在检查录像完整性")
        try:
            probe(part)
        except OSError:
            checking_media = True
            raise  # Missing tools, timeouts or unavailable storage can be retried locally.
        except CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc.__cause__, OSError):
                raise storage_error("读取媒体", exc.__cause__) from exc
            from .task_events import redact
            state.update(status="media_failed", reason=redact(str(exc))[:500])
            persist()
            raise DownloadedMediaError(
                "下载数据与回读一致，但媒体检查失败：" + redact(str(exc))[:500]
                + "；已保留临时录像，本次不自动重复下载。"
            ) from exc
        checking_media = False
        check_device()
        if before_replace:
            before_replace(output)
        os.replace(part, output)
        if on_verified:
            on_verified(digest)
        checkpoint.unlink(missing_ok=True)
        receipt.unlink(missing_ok=True)
        return output
    except requests.exceptions.Timeout:
        raise DownloadError("录像连接超时；重试时会检查并复用可续传进度。") from None
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError):
        raise DownloadError("录像连接中断；重试时会检查并复用可续传进度。") from None
    except requests.exceptions.RequestException:
        raise DownloadError("录像网络请求失败；重试时会检查并复用可续传进度。") from None
    except (StorageIntegrityError, OSError) as exc:
        if checking_media and isinstance(exc, OSError):
            raise  # Missing media tools/timeouts retain a locally retryable complete file.
        error = exc if isinstance(exc, StorageIntegrityError) else storage_error("写入", exc)
        if state is not None:
            state.update(status="storage_failed", reason=str(error)[:500])
            try:
                atomic_write_json(receipt, state, private=True)
            except OSError:
                pass
        if error is exc:
            raise
        raise error from exc
