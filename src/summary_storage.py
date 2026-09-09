"""Keep previous notes and legacy review exports in a private archive."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from .artifacts import file_sha256, internal_path


def archive_copy(path, root):
    checksum = file_sha256(path)
    target = root / checksum / path.name
    if target.is_file() and file_sha256(target) == checksum:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as dest, path.open("rb") as source:
            shutil.copyfileobj(source, dest)
            dest.flush()
            os.fsync(dest.fileno())
        if file_sha256(temporary) != checksum:
            raise OSError("笔记在归档过程中发生变化，请重试。")
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def archive_previous_note(path, replacement):
    """Keep the previous bytes before replacing the one visible note."""
    path = Path(path)
    if not path.is_file() or path.read_bytes() == replacement.encode("utf-8"):
        return
    root = path.parent / ".icourse" / "history"
    archive_copy(path, root)
    marker = internal_path(path.with_suffix(path.suffix + ".icourse.json"))
    if marker.is_file():
        archive_copy(marker, root)


def archive_legacy_review_exports(path, course_id, sub_id):
    """Relocate only this lecture's identified iCourse draft/report exports."""
    path = Path(path)
    folder = path.parent / "待核对"
    draft = folder / path.name
    root = path.parent / ".icourse" / "history"
    for old, kind in ((draft, "summary_draft"), (draft.with_suffix(".核对意见.md"), "review_report")):
        marker = internal_path(old.with_suffix(old.suffix + ".icourse.json"))
        if not old.is_file() or not marker.is_file():
            continue
        try:
            record = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or any(record.get(key) != value for key, value in dict(
                kind=kind, status="needs_review", course_id=course_id, sub_id=sub_id).items()):
            continue
        saved = archive_copy(old, root)
        archive_copy(marker, root)
        if file_sha256(old) != file_sha256(saved):
            raise OSError("旧版笔记在归档过程中发生变化，请重试。")
        old.unlink()
        marker.unlink()
    # Unrelated files (including another lecture's exports) stay in place.
    for directory in (folder / ".icourse", folder):
        try:
            directory.rmdir()
        except OSError:
            pass
