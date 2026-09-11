import errno
import os
from pathlib import Path

import pytest

from src.storage_access import StorageAccessError, check_directory


def deny_directory(monkeypatch, target):
    original = os.scandir

    def guarded(path):
        if not isinstance(path, int) and Path(path) == target:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)


def test_existing_directory_can_still_be_denied_by_macos(monkeypatch, tmp_path):
    folder = tmp_path / "Documents" / "courses"
    folder.mkdir(parents=True)
    deny_directory(monkeypatch, folder)
    monkeypatch.setattr("sys.platform", "darwin")
    with pytest.raises(StorageAccessError) as caught:
        check_directory(folder, "笔记保存位置", writable=True, create=True)
    assert str(folder) in str(caught.value)
    assert "Files & Folders" in str(caught.value)
    assert isinstance(caught.value.__cause__, PermissionError)


def test_probe_preserves_existing_files_and_leaves_no_temporary_file(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("existing note")
    check_directory(tmp_path, "笔记保存位置", writable=True, create=True)
    assert note.read_text() == "existing note"
    assert list(tmp_path.iterdir()) == [note]


@pytest.mark.parametrize("code,message", [(errno.EROFS, "只读"), (errno.ENOSPC, "空间不足")])
def test_write_probe_reports_storage_failure(monkeypatch, tmp_path, code, message):
    def fail(**kwargs):
        raise OSError(code, "storage error", str(tmp_path))
    monkeypatch.setattr("src.storage_access.tempfile.TemporaryFile", fail)
    with pytest.raises(StorageAccessError, match=message):
        check_directory(tmp_path, "笔记保存位置", writable=True)


def pipeline_args(monkeypatch, tmp_path, mode):
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("StuId", "test-user")
    monkeypatch.setenv("UISPsw", "not-real")
    monkeypatch.setattr("sys.argv", ["icourse", "--env-file", str(tmp_path / "no.env"),
        "--mode", mode, "--course-ids", "12345", "--out-dir", str(tmp_path / "videos"),
        "--summary-dir", str(tmp_path / "notes"), "--sleep", "0"])


def test_denied_notes_stop_before_login_or_paid_work(monkeypatch, tmp_path, capsys):
    from src import pipeline
    from src.summarizer import Summarizer
    pipeline_args(monkeypatch, tmp_path, "download_and_summarize")
    deny_directory(monkeypatch, tmp_path / "notes")
    monkeypatch.setattr(pipeline, "_login_with_retry", lambda *a, **k: pytest.fail("Unexpected login"))
    monkeypatch.setattr(Summarizer, "__init__", lambda *a, **k: pytest.fail("Unexpected LLM initialization"))
    assert pipeline.main() == 1
    output = capsys.readouterr().out
    assert "笔记保存位置" in output and "存储错误" in output
    assert "Traceback" not in output
    assert pipeline._STATE is None


def test_download_only_never_scans_protected_notes(monkeypatch, tmp_path):
    from src import pipeline
    from src.icourse import ICourseClient
    pipeline_args(monkeypatch, tmp_path, "download")
    video = tmp_path / "videos" / "12345-Test" / "录屏" / "课_123456.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"existing legacy video")
    deny_directory(monkeypatch, tmp_path / "notes")
    monkeypatch.setattr(pipeline, "_login_with_retry", lambda *a, **k: object())
    monkeypatch.setattr(ICourseClient, "get_course_detail", lambda *a, **k: dict(title="Test", lectures=[
        dict(sub_id="123456", sub_title="课", has_playback=True)]))
    monkeypatch.setattr(ICourseClient, "get_video_url", lambda *a, **k: pytest.fail("Unexpected download"))
    resolve = pipeline._resolve_course_dirs

    def guard_notes(path, *args):
        assert path != tmp_path / "notes", "Download-only must not discover notes"
        return resolve(path, *args)

    monkeypatch.setattr(pipeline, "_resolve_course_dirs", guard_notes)
    assert pipeline.main() == 0
    assert not (tmp_path / "notes").exists()
    assert video.read_bytes() == b"existing legacy video"


def test_denied_local_output_stops_before_transcription(monkeypatch, tmp_path, capsys):
    from src.cli import main
    from src.transcriber import Transcriber
    media = tmp_path / "lecture.wav"
    media.write_bytes(b"source")
    output = tmp_path / "output"
    deny_directory(monkeypatch, output)
    monkeypatch.setattr(Transcriber, "__init__", lambda *a, **k: pytest.fail("Unexpected ASR initialization"))
    assert main(["transcribe", str(media), "--output-dir", str(output)]) == 1
    assert "转录保存位置" in capsys.readouterr().err
