import json

import pytest

from src.artifacts import internal_path
from src.pipeline_state import artifact_metadata
from src.summary_storage import archive_legacy_review_exports


@pytest.mark.parametrize("marker_state", ["missing", "invalid", "other_course", "other_kind"])
def test_legacy_migration_preserves_unidentified_files(marker_state, tmp_path):
    target = tmp_path / "课_123456.md"
    draft = tmp_path / "待核对" / target.name
    draft.parent.mkdir()
    draft.write_text("应保留的文件")
    marker = internal_path(draft.with_suffix(".md.icourse.json"))
    if marker_state != "missing":
        artifact_metadata(draft, kind="other" if marker_state == "other_kind" else "summary_draft",
                          status="needs_review", course_id="other" if marker_state == "other_course" else "12345",
                          sub_id="123456")
        if marker_state == "invalid":
            marker.write_text("invalid JSON")
    before = marker.read_bytes() if marker.exists() else None
    archive_legacy_review_exports(target, "12345", "123456")
    assert draft.read_text() == "应保留的文件"
    assert (marker.read_bytes() if marker.exists() else None) == before


def test_legacy_migration_archives_user_edited_bytes_without_losing_them(tmp_path):
    target = tmp_path / "课_123456.md"
    draft = tmp_path / "待核对" / target.name
    draft.parent.mkdir()
    draft.write_text("原始生成的草稿")
    artifact_metadata(draft, kind="summary_draft", status="needs_review", course_id="12345", sub_id="123456")
    marker = internal_path(draft.with_suffix(".md.icourse.json"))
    original_record = json.loads(marker.read_text())
    draft.write_bytes(b"user edits: \xff\xfe")
    archive_legacy_review_exports(target, "12345", "123456")
    assert not draft.exists()
    archived = list((tmp_path / ".icourse/history").rglob("*.md"))
    assert len(archived) == 1 and archived[0].read_bytes() == b"user edits: \xff\xfe"
    markers = list((tmp_path / ".icourse/history").rglob("*.json"))
    assert len(markers) == 1 and json.loads(markers[0].read_text()) == original_record
