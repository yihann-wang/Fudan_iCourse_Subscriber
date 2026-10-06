"""Bounded readback checks for recordings written directly to their destination.

Only small receipts live in the application state directory. Media bytes never
pass through a system-disk staging file. Cache/flush controls are best effort;
they cannot make faulty hardware reliable.
"""

import errno
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from platformdirs import user_data_path

from .artifacts import atomic_write_json, file_signature

READ_BYTES = 1024 * 1024
SEGMENT_BYTES = 32 * 1024 * 1024
_UNSUPPORTED = {errno.EINVAL, errno.ENOTTY, errno.ENOSYS, errno.ENOTSUP}


class StorageIntegrityError(RuntimeError):
    """Do not retry network transfers after an untrustworthy local write/read."""


def checkpoint_path(output):
    root = Path(os.environ.get("ICOURSE_STATE_DIR") or user_data_path("Fudan iCourse", appauthor=False))
    key = hashlib.sha256(str(Path(output).absolute()).encode()).hexdigest()
    return root / "download-checks" / (key + ".json")


def _control(fd, operation):
    if sys.platform != "darwin":
        return False
    import fcntl

    try:
        fcntl.fcntl(fd, operation, 1 if operation == 48 else 0)
        return True
    except OSError as exc:
        if exc.errno in _UNSUPPORTED:
            return False
        raise


def flush_file(stream):
    stream.flush()
    os.fsync(stream.fileno())
    return _control(stream.fileno(), 51)  # Darwin F_FULLFSYNC


@contextmanager
def uncached_reader(path):
    with Path(path).open("rb", buffering=0) as stream:
        _control(stream.fileno(), 48)  # Darwin F_NOCACHE, scoped to this descriptor
        yield stream


def storage_error(action, exc):
    return StorageIntegrityError(
        f"保存位置{action}失败（{type(exc).__name__}，错误码 {getattr(exc, 'errno', None)}）。"
        "已保留现场；请检查磁盘、连接和可用空间后重新下载指定录像。"
    )


def checked_write_json(path, value):
    """Small destination-side records must round-trip too, not just the MP4."""
    expected = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    try:
        atomic_write_json(path, value, private=True)
        with Path(path).open("r+b") as stream:
            flush_file(stream)
        with uncached_reader(path) as stream:
            actual = stream.read(len(expected) + 1)
        if actual != expected:
            raise StorageIntegrityError("保存位置的校验记录写入后回读不一致；已停止下载，请检查磁盘及连接。")
    except OSError as exc:
        raise storage_error("写入校验记录", exc) from exc


def readback(path, segments, *, expected_size=None, progress=None, cancel=None):
    """Compare disk bytes with the hashes captured from network input.

Segments are (offset, byte count, SHA-256). Memory stays bounded regardless of
recording size. Returning a whole-file hash is meaningful for a full manifest.
"""
    digest = hashlib.sha256()
    try:
        before = file_signature(path)
        if expected_size is not None and before[2] != expected_size:
            raise StorageIntegrityError("录像回读长度与已接收长度不一致；已保留临时录像，请检查保存位置。")
        completed = 0
        total = sum(length for _, length, _ in segments)
        with uncached_reader(path) as stream:
            for offset, length, expected in segments:
                stream.seek(offset)
                part = hashlib.sha256()
                remaining = length
                while remaining:
                    if cancel is not None and cancel.is_set():
                        from .media import CancelledError
                        raise CancelledError("校验已取消。")
                    chunk = stream.read(min(READ_BYTES, remaining))
                    if not chunk:
                        raise StorageIntegrityError("录像回读提前结束；已保留临时录像，请检查保存位置。")
                    part.update(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                    completed += len(chunk)
                    if progress:
                        progress(completed, total)
                if part.hexdigest() != expected:
                    raise StorageIntegrityError(
                        f"录像写入后回读内容不一致（字节 {offset}–{offset + length}）。"
                        "已保留临时录像和接收摘要；请检查磁盘及连接后重新下载指定录像。"
                    )
        if file_signature(path) != before:
            raise StorageIntegrityError("录像在回读校验期间发生变化；已停止下载。")
        return digest.hexdigest()
    except OSError as exc:
        raise storage_error("回读", exc) from exc
