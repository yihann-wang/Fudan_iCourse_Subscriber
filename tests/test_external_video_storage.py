import hashlib
import io
import json
import os
import queue
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from src import video_checks, video_download, video_storage
from src.artifacts import internal_path
from src.video_download import DownloadedMediaError, DownloadError, _paths, download_video
from src.video_storage import StorageIntegrityError, checkpoint_path


@pytest.fixture
def body():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(bytes(range(256)) * 128)
    return output.getvalue()


def response(body, headers=None, status=200):
    return SimpleNamespace(status_code=status, headers=headers or {
        "Content-Length": str(len(body)), "ETag": '"version"'},
        iter_content=lambda **kw: iter([body]), close=Mock())


def test_closed_gui_output_does_not_fail_a_verified_download(tmp_path, body, monkeypatch):
    from src.pipeline import _download_video_with_progress

    class ClosedPipe:
        def write(self, text):
            raise BrokenPipeError("GUI exited")

        def flush(self):
            raise BrokenPipeError("GUI exited")

    video = tmp_path / "lecture.mp4"
    client = SimpleNamespace(get_video_response=Mock(return_value=response(body)))
    with monkeypatch.context() as patch:
        patch.setattr("sys.stdout", ClosedPipe())
        result = _download_video_with_progress(client, "https://example.invalid/video", video)
    assert result == video and video.read_bytes() == body
    assert video_checks.read_marker(video_checks.record_path(video))["status"] == "verified"
    client.get_video_response.assert_called_once()


def partial(video, body):
    def content(**kw):
        yield body[:512]
        raise requests.exceptions.ChunkedEncodingError("https://private.invalid/?token=secret")
    r = response(body, {"Content-Range": f"bytes 0-1023/{len(body)}", "ETag": '"version"'}, 206)
    r.iter_content = content
    client = SimpleNamespace(get_video_response=Mock(return_value=r))
    with pytest.raises(DownloadError, match="连接中断"):
        download_video(client, "https://example.invalid/video", video)
    return client


def test_resume_rejects_changed_bytes_before_any_network_request(tmp_path, body):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"keep old recording")
    client = partial(video, body)
    part, _ = _paths(video)
    before = part.stat()
    part.write_bytes(bytes(512))
    os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
    client.get_video_response.reset_mock()
    with pytest.raises(StorageIntegrityError, match="回读内容不一致"):
        download_video(client, "https://example.invalid/video", video)
    client.get_video_response.assert_not_called()
    saved = json.loads(checkpoint_path(video).read_text())
    assert saved["segments"][0][2] == hashlib.sha256(body[:512]).hexdigest()
    assert saved["status"] == "storage_failed"
    assert video.read_bytes() == b"keep old recording" and part.exists()


@pytest.mark.parametrize("when", ["segment", "complete"])
def test_bad_readback_never_replaces_existing_recording(tmp_path, body, monkeypatch, when):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"old recording")
    real = video_download.readback
    def corrupt(path, segments, **kwargs):
        if (kwargs.get("expected_size") is not None) == (when == "complete"):
            with Path(path).open("r+b") as stream:
                stream.seek(100)
                stream.write(b"bad!")
        return real(path, segments, **kwargs)
    monkeypatch.setattr(video_download, "readback", corrupt)
    client = SimpleNamespace(get_video_response=Mock(return_value=response(body)))
    with pytest.raises(StorageIntegrityError, match="回读内容不一致"):
        download_video(client, "https://example.invalid/video", video)
    assert video.read_bytes() == b"old recording"
    assert _paths(video)[0].exists()
    client.get_video_response.assert_called_once()


def test_corrupted_destination_json_is_a_storage_failure(tmp_path, body, monkeypatch):
    video = tmp_path / "lecture.mp4"
    mirror = _paths(video)[1]
    real = video_storage.atomic_write_json
    def corrupt(path, value, **kwargs):
        real(path, value, **kwargs)
        if path == mirror:
            path.write_bytes(bytes(path.stat().st_size))
    monkeypatch.setattr(video_storage, "atomic_write_json", corrupt)
    client = SimpleNamespace(get_video_response=Mock(return_value=response(body)))
    with pytest.raises(StorageIntegrityError, match="校验记录写入后回读不一致"):
        download_video(client, "https://example.invalid/video", video)
    assert not video.exists()
    assert json.loads(checkpoint_path(video).read_text())["status"] == "storage_failed"


def test_crash_tail_is_discarded_after_verifying_committed_prefix(tmp_path, body):
    video = tmp_path / "lecture.mp4"
    client = partial(video, body)
    with _paths(video)[0].open("ab") as stream:
        stream.write(b"uncommitted tail")
    headers = {"Content-Range": f"bytes 512-{len(body)-1}/{len(body)}", "ETag": '"version"'}
    client.get_video_response = Mock(return_value=response(body[512:], headers, 206))
    download_video(client, "https://example.invalid/video", video)
    assert client.get_video_response.call_args.kwargs["headers"]["Range"].startswith("bytes=512-")
    assert video.read_bytes() == body
    assert not checkpoint_path(video).exists()


def test_complete_receipt_survives_crash_before_complete_flag(tmp_path, body, monkeypatch):
    video = tmp_path / "lecture.mp4"
    client = SimpleNamespace(get_video_response=Mock(return_value=response(body)))
    real_probe = __import__("src.media", fromlist=["probe"]).probe
    monkeypatch.setattr("src.media.probe", Mock(side_effect=FileNotFoundError("ffprobe")))
    with pytest.raises(FileNotFoundError):
        download_video(client, "https://example.invalid/video", video)
    record = checkpoint_path(video)
    saved = json.loads(record.read_text())
    saved["complete"] = False
    record.write_text(json.dumps(saved))
    monkeypatch.setattr("src.media.probe", real_probe)
    download_video(client, "https://example.invalid/video", video)
    assert video.read_bytes() == body
    client.get_video_response.assert_called_once()


def test_old_unhashed_checkpoint_restarts_from_zero(tmp_path, body):
    video = tmp_path / "lecture.mp4"
    part, mirror = _paths(video)
    part.parent.mkdir()
    part.write_bytes(b"untrusted bytes")
    mirror.write_text(json.dumps(dict(schema=1, validator=["etag", '"version"'], total=len(body))))
    messages = []
    client = SimpleNamespace(get_video_response=Mock(return_value=response(body)))
    download_video(client, "https://example.invalid/video", video, message=messages.append)
    assert client.get_video_response.call_args.kwargs["headers"]["Range"].startswith("bytes=0-")
    assert any("缺少可信" in m for m in messages)
    assert video.read_bytes() == body


@pytest.mark.parametrize("failure", [StorageIntegrityError("write failed"), DownloadedMediaError("bad media")])
def test_storage_failure_stops_queue_but_bad_remote_media_only_stops_one_task(tmp_path, monkeypatch, failure):
    from src.pipeline import _Counters, _download_stage
    incoming, outgoing = queue.Queue(), queue.Queue()
    for sub in ("123456", "123457"):
        incoming.put(dict(course_id="12345", sub_id=sub, target_video_path=tmp_path / f"{sub}.mp4"))
    incoming.put(None)
    download = Mock(side_effect=[failure, tmp_path / "123457.mp4"])
    monkeypatch.setattr("src.pipeline._download_video_with_progress", download)
    client = SimpleNamespace(get_video_url=Mock(return_value="https://example.invalid/video"), check_alive=Mock())
    counters = _Counters()
    _download_stage(incoming, outgoing, client, 0, counters)
    stopped = isinstance(failure, StorageIntegrityError)
    assert download.call_count == (1 if stopped else 2)
    assert counters.failed == (2 if stopped else 1)
    assert counters.downloaded == (0 if stopped else 1)
    client.check_alive.assert_not_called()


def test_manual_check_uses_system_digest_when_external_marker_is_damaged(tmp_path, body):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    video_checks.remember(video, "verified", digest=expected, method="download_readback")
    marker = internal_path(Path(str(video) + ".icourse.json"))
    marker.parent.mkdir()
    marker.write_bytes(bytes(192))
    assert "校验记录损坏" in video_checks.quick_status(video, tmp_path)
    # Still a decodable WAV, but no longer the bytes received originally.
    video.write_bytes(body[:-4] + b"oops")
    with pytest.raises(RuntimeError, match="原接收摘要不一致"):
        video_checks.verify_video(video, tmp_path)
    assert json.loads(video_checks.record_path(video).read_text())["sha256"] == expected
    video.write_bytes(body)
    assert video_checks.verify_video(video, tmp_path) == expected
    assert video_checks.quick_status(video, tmp_path) == ""
    assert marker.read_bytes() == bytes(192)


@pytest.mark.parametrize("resume", [False, True])
def test_explicit_repair_downloads_bad_existing_video_without_paid_work(tmp_path, monkeypatch, body, resume):
    from src.icourse import ICourseClient
    from src.pipeline import main
    from src.summarizer import Summarizer
    from src.transcriber import Transcriber

    root = tmp_path / "videos"
    video = root / "12345-Course/录屏/课_123456.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"bad video")
    marker = internal_path(Path(str(video) + ".icourse.json"))
    marker.parent.mkdir()
    marker.write_bytes(bytes(192))
    note = tmp_path / "notes/12345-Course/笔记/课_123456.md"
    note.parent.mkdir(parents=True)
    note.write_text("existing notes")
    monkeypatch.setenv("StuId", "test")
    monkeypatch.setenv("UISPsw", "test")
    monkeypatch.setattr("src.pipeline._login_with_retry", lambda *a, **k: object())
    monkeypatch.setattr(ICourseClient, "get_course_detail", lambda *a: dict(title="Course", lectures=[
        dict(sub_id="123456", sub_title="课", has_playback=True)]))
    monkeypatch.setattr(ICourseClient, "get_video_url", lambda *a: "https://example.invalid/video")
    monkeypatch.setattr(ICourseClient, "get_video_response", lambda *a, **k: response(body))
    monkeypatch.setattr(Summarizer, "__init__", lambda self: None)
    for obj, name in ((Summarizer, "summarize"), (Transcriber, "transcribe_result")):
        monkeypatch.setattr(obj, name, lambda *a, **k: pytest.fail("Paid work during recording repair"))
    mode = "download_and_summarize" if resume else "download"
    flags = ["--target", "12345:123456", "--resume-stage", "12345:123456:dl"] if resume else ["--sub-ids", "123456", "--redownload"]
    monkeypatch.setattr("sys.argv", ["icourse", "--mode", mode, "--course-ids", "12345",
                        "--out-dir", str(root), "--summary-dir", str(tmp_path / "notes"), "--sleep", "0", *flags])
    assert main() == 0
    assert video.read_bytes() == body and note.read_text() == "existing notes"
    assert json.loads(video_checks.record_path(video).read_text())["sha256"] == hashlib.sha256(body).hexdigest()


def test_retry_failed_download_keeps_verified_network_partial(tmp_path, monkeypatch, body):
    from src.icourse import ICourseClient
    from src.pipeline import main
    root = tmp_path / "videos"
    video = root / "12345-Course/录屏/课_123456.mp4"
    video.parent.mkdir(parents=True)
    partial(video, body)
    calls = []
    def get(self, url, **kwargs):
        calls.append(kwargs["headers"]["Range"])
        return response(body[512:], {"Content-Range": f"bytes 512-{len(body)-1}/{len(body)}", "ETag": '"version"'}, 206)
    monkeypatch.setattr(ICourseClient, "get_video_response", get)
    monkeypatch.setattr(ICourseClient, "get_video_url", lambda *a: "https://example.invalid/video")
    monkeypatch.setattr(ICourseClient, "get_course_detail", lambda *a: dict(title="Course", lectures=[
        dict(sub_id="123456", sub_title="课", has_playback=True)]))
    monkeypatch.setattr("src.pipeline._login_with_retry", lambda *a, **kw: object())
    monkeypatch.setenv("StuId", "test")
    monkeypatch.setenv("UISPsw", "test")
    monkeypatch.setattr("sys.argv", ["icourse", "--mode", "download", "--course-ids", "12345",
                        "--out-dir", str(root), "--sleep", "0", "--target", "12345:123456",
                        "--resume-stage", "12345:123456:dl"])
    assert main() == 0
    assert len(calls) == 1 and calls[0].startswith("bytes=512-")
    assert video.read_bytes() == body


def test_write_io_failure_preserves_original_and_stops_retry(tmp_path, monkeypatch, body):
    import errno
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"original")
    monkeypatch.setattr(video_download, "flush_file", Mock(side_effect=OSError(errno.ENOSPC, "no space")))
    client = SimpleNamespace(get_video_response=Mock(return_value=response(body)))
    with pytest.raises(StorageIntegrityError, match="写入失败"):
        download_video(client, "https://example.invalid/video", video)
    assert video.read_bytes() == b"original"
    assert _paths(video)[0].exists()
    assert json.loads(checkpoint_path(video).read_text())["status"] == "storage_failed"


def test_media_diagnostic_retains_reason_and_removes_credentials(tmp_path, monkeypatch):
    from src.media import probe
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"bad video")
    monkeypatch.setattr("src.media.run_media", lambda *a, **kw: (1, b"{}", b"moov atom not found\nhttps://secret.invalid/?token=123\nAuthorization: Bearer secret"))
    with pytest.raises(RuntimeError) as error:
        probe(p)
    text = str(error.value)
    assert "moov atom not found" in text and "退出码 1" in text
    assert "secret" not in text and "token=123" not in text


def test_gui_repair_requires_selection_and_forces_download_only(tmp_path):
    from PySide6.QtWidgets import QApplication
    from src.mac_gui import MainWindow
    from src.preferences import defaults
    app = QApplication.instance() or QApplication([])
    values = dict(defaults(), course_ids="12345", sub_ids="123456", stu_id="fake", uis_psw="fake")
    window = MainWindow(initial_values=values)
    args = window.command("redownload-videos", values)
    assert args[args.index("--mode") + 1] == "download" and "--redownload" in args
    with pytest.raises(ValueError, match="指定课次"):
        window.command("redownload-videos", dict(values, sub_ids=""))
    window.close()
    app.processEvents()
