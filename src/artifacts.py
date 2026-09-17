"""Crash-safe file writes and stable local media identities."""

import hashlib
import json
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from platformdirs import user_cache_path


def atomic_write_text(path, text: str, *, private: bool = False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if private:
            os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def atomic_write_json(path, value, *, private=False):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", private=private)


def internal_path(path):
    """Keep machine-readable companions out of the user's visible output list."""
    path = Path(path)
    return path.parent / ".icourse" / path.name


def migrate_auxiliary(path):
    """Move a known JSON companion, retaining conflicting files for inspection."""
    path = Path(path)
    target = internal_path(path)
    if path.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        path.rename(target)
    return target


def migrate_course_metadata(course_dir):
    """Only relocate iCourse companions; never arbitrary user JSON files."""
    course_dir = Path(course_dir)
    moved = []
    for folder in (course_dir, *(course_dir / name for name in ("录屏", "笔记", "原始txt"))):
        for pattern in ("*.icourse.json", "*.segments.json"):
            for path in folder.glob(pattern):
                if path.name.endswith(".icourse.json"):
                    source = path.with_name(path.name.removesuffix(".icourse.json"))
                else:
                    source = path.with_name(path.name.removesuffix(".segments.json") + ".txt")
                if source.is_file():
                    target = migrate_auxiliary(path)
                    if not path.exists():
                        moved.append(target)
    return moved


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=512)
def _cached_hash(path, signature, cache_root, verify=False):
    # Tiny text files are cheap to verify; persist hashes only for large media.
    record = None
    if signature[2] >= 8 * 1024 * 1024:
        record = Path(cache_root) / (hashlib.sha256(path.encode()).hexdigest() + ".json")
        try:
            saved = json.loads(record.read_text())
            digest = saved.get("sha256", "")
            if (not verify and saved.get("signature") == list(signature) and isinstance(digest, str)
                    and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
                return digest
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    digest = file_sha256(path)
    if file_signature(path) != signature:
        raise RuntimeError("文件在校验期间发生变化，请等文件保存完成后再重试。")
    if record is not None:
        try:
            atomic_write_json(record, dict(signature=signature, sha256=digest), private=True)
        except OSError:
            pass  # A cache failure must not invalidate a successfully checked file.
    return digest


def file_signature(path):
    stat = Path(path).stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def cached_file_sha256(path):
    path = Path(path).resolve()
    root = os.environ.get("ICOURSE_HASH_CACHE_DIR") or str(
        user_cache_path("Fudan iCourse Subscriber", appauthor=False) / "file-hashes")
    return _cached_hash(str(path), file_signature(path), root, os.environ.get("ICOURSE_VERIFY_FILES") == "1")


def local_media_id(path):
    # Content identity survives renames and moves; never infer an online ID
    # from a non-unique lecture title. Prefix distinguishes it from server IDs.
    return "local-" + cached_file_sha256(path)[:24]
