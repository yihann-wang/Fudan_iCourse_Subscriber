"""macOS resource-fork companions on USB volumes are never course media."""

import pytest

from src import pipeline, video_checks


@pytest.mark.parametrize("name", ["课_123456.mp4", "123456_课.mp4", "123456.mp4"])
def test_media_scans_ignore_resource_fork_and_keep_real_recording(tmp_path, monkeypatch, name):
    course = tmp_path / "12345-Course"
    course.mkdir()
    video = course / name
    video.write_bytes(b"recording")
    companion = course / ("._" + name)
    companion.write_bytes(b"\x00\x05\x16\x07resource fork")
    checked = []

    def validate(path):
        checked.append(path)
        return True

    monkeypatch.setattr(pipeline, "valid_artifact", validate)
    assert video_checks.find_video(tmp_path, "12345", "123456") == (video, "")
    assert pipeline._find_file_for_lecture([course], "课", "123456", ".mp4") == video
    assert checked == [video]
    assert pipeline._collect_local_lectures([course], set())[0]["local_path"] == video
    assert companion.read_bytes() == b"\x00\x05\x16\x07resource fork"

    video.unlink()
    assert video_checks.find_video(tmp_path, "12345", "123456") == (None, "")
    assert pipeline._scan_downloaded_sub_ids([course]) == set()
    assert pipeline._collect_local_lectures([course], set()) == []


@pytest.mark.parametrize("suffix", [".mp4", ".md", ".txt"])
def test_orphan_resource_forks_are_not_reused_or_migrated(tmp_path, monkeypatch, suffix):
    companion = tmp_path / ("._课_123456" + suffix)
    companion.write_bytes(b"resource fork")
    monkeypatch.setattr(pipeline, "valid_artifact", lambda path: pytest.fail("Read resource fork"))
    assert pipeline._scan_existing_sub_ids([tmp_path], "*" + suffix) == set()
    assert pipeline._find_file_for_lecture([tmp_path], "课", "123456", suffix) is None
    pipeline._move_legacy_artifacts_to_layout(tmp_path)
    assert companion.read_bytes() == b"resource fork"


def test_bare_title_resource_fork_is_not_a_second_local_lecture(tmp_path):
    video = tmp_path / "没有课次编号.mp4"
    video.write_bytes(b"recording")
    (tmp_path / ("._" + video.name)).write_bytes(b"resource fork")
    lectures = pipeline._collect_local_lectures([tmp_path], set())
    assert len(lectures) == 1 and lectures[0]["local_path"] == video
