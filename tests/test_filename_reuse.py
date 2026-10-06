import io
import json
import threading
import wave

import pytest

from src import video_checks as checks
from src.artifacts import internal_path
from src.media import CancelledError


@pytest.fixture
def video(tmp_path, monkeypatch):
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "state"))
    p = tmp_path / "videos" / "12345-Course" / "录屏" / "课_123456.mp4"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"unchecked video")
    return p


def root(video):
    return video.parents[2]


def marker(video, content):
    p = internal_path(video.with_name(video.name + ".icourse.json"))
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(content)
    return p


def test_quick_reuse_never_reads_video_or_checks_hash(video, monkeypatch):
    for fn in [
        "src.artifacts.file_sha256",
        "src.artifacts.cached_file_sha256",
        "src.media.probe",
        "src.media_integrity.check_mp4_completeness",
    ]:
        monkeypatch.setattr(fn, lambda *a, **k: pytest.fail("Unexpected full check"))
    from pathlib import Path

    original = Path.open

    def checked(path, *a, **kw):
        assert path != video, "Quick reuse opened video bytes"
        return original(path, *a, **kw)

    monkeypatch.setattr(Path, "open", checked)
    for _ in range(2):
        assert checks.find_video(root(video), "12345", "123456") == (video, "")
    assert checks.find_video(root(video), "99999", "123456") == (None, "")
    assert checks.find_video(root(video), "12345", "12345") == (None, "")


@pytest.mark.parametrize(
    "content",
    [bytes(185), b"{broken", b'{"status":"invalid"}', b'{"status":"needs_review"}'],
)
def test_known_bad_marker_is_retained(video, content):
    m = marker(video, content)
    assert checks.find_video(root(video), "12345", "123456")[1]
    assert m.read_bytes() == content


def test_empty_symlink_partial_and_wrong_course_are_not_reused(video, tmp_path):
    video.write_bytes(b"")
    assert checks.find_video(root(video), "12345", "123456")[1]
    video.unlink()
    part = video.with_suffix(".mp4.download.part")
    part.write_bytes(b"partial")
    assert checks.find_video(root(video), "12345", "123456") == (None, "")
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"video")
    video.symlink_to(outside)
    assert checks.find_video(root(video), "12345", "123456")[1]
    assert outside.read_bytes() == b"video" and part.exists()


def test_course_symlink_is_blocked(video, tmp_path):
    link = root(video) / "22222-Other"
    link.symlink_to(video.parents[1], target_is_directory=True)
    assert checks.find_video(root(video), "22222", "123456")[1]


def test_failed_history_blocks_existing_file(video):
    from src.pipeline_state import PipelineState

    state = PipelineState(checks.state_root() / "pipeline.sqlite3")
    queue = state.queue("download")
    queue.put(dict(course_id="12345", sub_id="123456", target_video_path=video))
    queue.get()
    queue.mark("failed", "IncompleteMediaError: 录像未下载完整")
    state.close()
    assert "失败记录" in checks.find_video(root(video), "12345", "123456")[1]


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        f.writeframes(bytes(32000))
    return out.getvalue()


def test_manual_full_verification_progress_and_repeat(video):
    video.write_bytes(wav_bytes())
    progress = []
    digest = checks.verify_video(
        video, root(video), progress=lambda *x: progress.append(x)
    )
    assert len(digest) == 64 and progress[-1] == (video.stat().st_size,) * 2
    assert checks.verify_video(video, root(video)) == digest
    assert checks.quick_status(video, root(video)) == ""


def test_failed_manual_check_and_cancel_preserve_file_and_failure(video):
    before = video.read_bytes()
    with pytest.raises(RuntimeError):
        checks.verify_video(video, root(video))
    saved = checks.record_path(video).read_bytes()
    assert checks.quick_status(video, root(video))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        checks.verify_video(video, root(video), cancel=cancel)
    assert (
        video.read_bytes() == before and checks.record_path(video).read_bytes() == saved
    )


def test_cancel_during_hash_does_not_mark_success(video, monkeypatch):
    video.write_bytes(wav_bytes() * 80)
    event = threading.Event()
    monkeypatch.setattr(
        "src.media.probe",
        lambda *a, **k: pytest.fail("Cancelled verification reached probe"),
    )
    with pytest.raises(CancelledError):
        checks.verify_video(
            video, root(video), cancel=event, progress=lambda *a: event.set()
        )
    assert not checks.record_path(video).exists()


def test_manual_command_never_logs_in_or_calls_cloud(video, monkeypatch):
    from src.cli import main

    video.write_bytes(wav_bytes())
    monkeypatch.setattr(
        "src.webvpn.WebVPNSession.login", lambda *a, **k: pytest.fail("Login")
    )
    assert (
        main(["verify-videos", "--out-dir", str(root(video)), "--course-ids", "12345"])
        == 0
    )


@pytest.mark.parametrize("bad", [False, True])
@pytest.mark.parametrize("mode", ["download", "download_and_summarize"])
def test_pipeline_quick_skip_no_hash_media_or_paid_work(
    video, tmp_path, monkeypatch, capsys, bad, mode
):
    from src.icourse import ICourseClient
    from src.pipeline import main
    from src.summarizer import Summarizer
    from src.task_view_model import TaskViewModel
    from src.transcriber import Transcriber

    monkeypatch.setenv("ICOURSE_EVENTS", "json")
    monkeypatch.setenv("StuId", "test")
    monkeypatch.setenv("UISPsw", "test")
    monkeypatch.setattr("src.pipeline._login_with_retry", lambda *a, **k: object())
    monkeypatch.setattr(
        ICourseClient,
        "get_course_detail",
        lambda *a: dict(
            title="Course",
            lectures=[dict(sub_id="123456", sub_title="课", has_playback=True)],
        ),
    )
    monkeypatch.setattr(Summarizer, "__init__", lambda self: None)
    for obj, name in [
        (ICourseClient, "get_video_url"),
        (Transcriber, "transcribe_result"),
        (Summarizer, "summarize"),
    ]:
        monkeypatch.setattr(
            obj, name, lambda *a, **k: pytest.fail("Unexpected network/paid work")
        )
    for fn in [
        "src.pipeline_state.cached_file_sha256",
        "src.artifacts.file_sha256",
        "src.media.probe",
        "src.pipeline_state.check_mp4_completeness",
    ]:
        monkeypatch.setattr(fn, lambda *a, **k: pytest.fail("Unexpected media read"))
    notes = tmp_path / "notes" / "12345-Course" / "笔记" / "课_123456.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("existing notes")
    if bad:
        marker(video, bytes(192))
    monkeypatch.setattr(
        "sys.argv",
        [
            "icourse",
            "--mode",
            mode,
            "--course-ids",
            "12345",
            "--out-dir",
            str(root(video)),
            "--summary-dir",
            str(tmp_path / "notes"),
            "--sleep",
            "0",
        ],
    )
    for _ in range(2):
        assert main() == int(bad)
        out = capsys.readouterr().out
        rows = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        model = TaskViewModel(rows[0]["run_id"])
        for row in rows:
            model.apply(row)
        model.finish(int(bad))
        expected = "failed" if bad else "skipped" if mode == "download" else "success"
        assert model.counts()[expected] == 1
    assert notes.read_text() == "existing notes"


def test_gui_manual_verification_requires_no_credentials_and_has_cancel(
    video, monkeypatch
):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from src.mac_gui import MainWindow
    from src.preferences import defaults

    app = QApplication.instance() or QApplication([])
    values = defaults()
    values.update(course_ids="12345", out_dir=str(root(video)), sub_ids="123456")
    window = MainWindow(initial_values=values)
    command = window.command("verify-videos", values)
    assert command == [
        "verify-videos",
        "--out-dir",
        str(root(video)),
        "--course-ids",
        "12345",
        "--sub-ids",
        "123456",
    ]
    assert "完整校验" in window.verify.text() and window.stop.text() == "停止任务"
    window.panel.begin("verify-test")
    from src.task_view_model import TaskViewModel

    model = TaskViewModel("verify-test")
    model.apply(
        dict(
            event="icourse",
            version=1,
            run_id="verify-test",
            seq=1,
            kind="task",
            course_id="12345",
            sub_id="123456",
            stages=dict(dl="running", tr="na", sm="na"),
        )
    )
    model.finish(143, cancelled=True)
    assert model.counts()["cancelled"] == 1 and model.counts()["success"] == 0
    window.close()
    app.processEvents()


def test_new_download_still_rejects_invalid_media_and_preserves_existing(
    video, monkeypatch
):
    from types import SimpleNamespace

    from src.video_download import download_video

    before = video.read_bytes()
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Length": "11"},
        iter_content=lambda **kw: iter([b"not a video"]),
        close=lambda: None,
    )
    client = SimpleNamespace(get_video_response=lambda *a, **kw: response)
    with pytest.raises(RuntimeError, match="无法读取媒体"):
        download_video(client, "https://example.invalid/video", video)
    assert video.read_bytes() == before


def test_newer_failure_marker_overrides_old_verified_record(video):
    import os

    checks.remember(video, "verified")
    m = marker(video, b'{"status":"invalid"}')
    rec = json.loads(checks.record_path(video).read_text())
    os.utime(m, (rec["checked_at"] + 1, rec["checked_at"] + 1))
    assert checks.quick_status(video, root(video))


def test_no_formal_video_does_not_consume_partial(video):
    video.unlink()
    partial = internal_path(video.with_name(video.name + ".download.part"))
    partial.parent.mkdir(exist_ok=True)
    partial.write_bytes(b"partial bytes")
    checkpoint = partial.with_suffix(".json")
    checkpoint.write_bytes(bytes(185))
    assert checks.find_video(root(video), "12345", "123456") == (None, "")
    assert (
        partial.read_bytes() == b"partial bytes"
        and checkpoint.read_bytes() == bytes(185)
    )


def test_offscreen_gui_runs_manual_verify_without_credentials(video, monkeypatch):
    import time

    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication

    from src.mac_gui import MainWindow
    from src.preferences import defaults

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    video.write_bytes(wav_bytes())
    app = QApplication.instance() or QApplication([])
    values = defaults()
    values.update(course_ids="12345", out_dir=str(root(video)))
    window = MainWindow(initial_values=values)
    window.launch("verify-videos")
    end = time.monotonic() + 15
    while window.exit_result is None and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert window.process.state() == QProcess.ProcessState.NotRunning
    assert window.exit_result == (0, False)
    assert window.panel.model.counts()["success"] == 1
    assert window.verify.isEnabled() and not window.retry.isEnabled()
    assert json.loads(checks.record_path(video).read_text())["status"] == "verified"
    window.close()


def test_unrelated_later_failure_does_not_hide_media_failure(video):
    from src.pipeline_state import PipelineState

    state = PipelineState(checks.state_root() / "pipeline.sqlite3")
    for error in ["IncompleteMediaError: 录像未下载完整", "请求超时"]:
        q = state.queue("download")
        q.put(dict(course_id="12345", sub_id="123456", target_video_path=video))
        q.get()
        q.mark("failed", error)
    state.close()
    assert checks.find_video(root(video), "12345", "123456")[1]


def test_legacy_filename_stays_in_place(video):
    old = video.parents[1] / "123456_旧课.mp4"
    video.rename(old)
    assert checks.find_video(root(video), "12345", "123456") == (old, "")
    assert old.exists() and not video.exists()


def test_duplicate_files_are_not_silently_selected(video):
    another = video.with_name("另一标题_123456.mp4")
    another.write_bytes(b"another")
    assert "多个录像" in checks.find_video(root(video), "12345", "123456")[1]


def test_known_bad_blocked_before_transcriber_factory(video):
    import queue

    from src.pipeline import _Counters, _transcribe_stage

    marker(video, bytes(192))
    q = queue.Queue()
    q.put(
        dict(
            course_id="12345",
            sub_id="123456",
            video_path=video,
            target_transcript_path=video.with_suffix(".txt"),
        )
    )
    q.put(None)
    counters = _Counters()
    _transcribe_stage(
        q,
        None,
        lambda: pytest.fail("No paid transcriber should be constructed"),
        counters,
    )
    assert counters.failed == 1
