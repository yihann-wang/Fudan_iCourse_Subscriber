import json

from src.artifacts import internal_path, migrate_auxiliary, migrate_course_metadata
from src.pipeline_state import artifact_metadata, valid_artifact


def test_course_migration_preserves_bytes_and_leaves_unrelated_json(tmp_path):
    course = tmp_path / "37744-课程"
    raw = course / "原始txt"
    raw.mkdir(parents=True)
    transcript = raw / "课_654415.txt"
    transcript.write_text("原始文本")
    marker = transcript.with_suffix(".txt.icourse.json")
    marker.write_text('{"schema":1,"status":"writing"}')
    segments = transcript.with_suffix(".segments.json")
    segments.write_text('{"segments":[{"start":0,"end":1,"text":"测试"}]}')
    unrelated = raw / "用户参数.json"
    unrelated.write_text('{"keep":true}')
    original = {p.name: p.read_bytes() for p in (marker, segments)}
    assert len(migrate_course_metadata(course)) == 2
    assert all(not p.exists() and internal_path(p).read_bytes() == original[p.name] for p in (marker, segments))
    assert unrelated.exists() and transcript.read_text() == "原始文本"
    assert migrate_course_metadata(course) == []
    assert not valid_artifact(transcript)  # A migrated pending marker stays pending.


def test_existing_legacy_marker_is_honored_without_writing_then_migrated(tmp_path):
    video = tmp_path / "课_654415.mp4"
    video.write_bytes(b"video")
    artifact_metadata(video)
    old = video.with_suffix(".mp4.icourse.json")
    internal_path(old).rename(old)
    assert valid_artifact(video)
    assert old.exists()
    migrate_auxiliary(old)
    assert not old.exists() and internal_path(old).exists()
    video.write_bytes(b"damage")
    assert not valid_artifact(video)


def test_auxiliary_conflict_never_overwrites_either_copy(tmp_path):
    legacy = tmp_path / "note.md.icourse.json"
    legacy.write_text("old")
    hidden = internal_path(legacy)
    hidden.parent.mkdir()
    hidden.write_text("new")
    assert migrate_auxiliary(legacy) == hidden
    assert legacy.read_text() == "old" and hidden.read_text() == "new"


def test_successful_notes_survive_budget_change_but_not_source_change(tmp_path, monkeypatch):
    from src.asr import ASRSettings
    transcript = tmp_path / "课_654415.txt"
    transcript.write_text("原始文本")
    artifact_metadata(transcript, settings_fingerprint=ASRSettings.from_env().fingerprint)
    notes = tmp_path / "课_654415.md"
    notes.write_text("已经完成的笔记")
    artifact_metadata(notes, source=transcript, settings_fingerprint="older-budget-and-prompt")
    monkeypatch.setenv("LLM_INPUT_CHAR_LIMIT", "8000")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "32768")
    assert valid_artifact(notes)
    marker = internal_path(notes.with_suffix(".md.icourse.json"))
    assert json.loads(marker.read_text())["settings_fingerprint"] == "older-budget-and-prompt"
    transcript.write_text("修改后的原始文本")
    assert not valid_artifact(notes)


def test_flat_transcript_timecodes_move_with_text_not_video(tmp_path):
    from src.pipeline import _move_legacy_artifacts_to_layout
    course = tmp_path / "12345-课程"
    course.mkdir()
    video, transcript = course / "课_123456.mp4", course / "课_123456.txt"
    video.write_bytes(b"video")
    transcript.write_text("原文")
    segments = transcript.with_suffix(".segments.json")
    segments.write_text('{"segments": []}')
    pending = transcript.with_suffix(".txt.icourse.json")
    pending.write_text('{"status":"writing"}')
    _move_legacy_artifacts_to_layout(course)
    assert internal_path(course / "原始txt" / segments.name).is_file()
    assert not internal_path(course / "录屏" / segments.name).exists()
    assert not valid_artifact(course / "原始txt" / transcript.name)
