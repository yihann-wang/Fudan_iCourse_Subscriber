import io
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.media import probe
from src.media_integrity import IncompleteMediaError, check_mp4_completeness
from src.pipeline_state import artifact_metadata, valid_artifact


def box(kind, payload=b"", *, extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def recording():
    return box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov") + box(b"mdat", b"a" * 128)


@pytest.mark.parametrize("metadata", [False, True])
def test_truncated_recording_is_rejected_even_with_matching_saved_hash(tmp_path, metadata):
    video = tmp_path / "lesson_123456.mp4"
    video.write_bytes(recording()[:-64])
    if metadata:
        artifact_metadata(video, kind="video")  # What older versions saved.
    assert not valid_artifact(video)
    with pytest.raises(IncompleteMediaError, match="录像未下载完整"):
        check_mp4_completeness(video)
    assert video.exists()  # Never delete the user's recording during inspection.


@pytest.mark.parametrize("payload", [
    recording(),
    box(b"ftyp") + box(b"mdat", b"data", extended=True),
    box(b"ftyp") + struct.pack(">I4s", 0, b"mdat") + b"data",
    box(b"ftyp") + box(b"uuid", b"0" * 16) + box(b"mdat"),
    box(b"moov") + box(b"mdat") + box(b"free"),
    b"RIFF" + b"0" * 32,  # Non-MP4 containers are still handled by ffprobe.
])
def test_complete_box_boundaries_and_other_containers(tmp_path, payload):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(payload)
    check_mp4_completeness(video)
    assert valid_artifact(video)


@pytest.mark.parametrize("tail", [
    b"tail",  # Incomplete 8-byte header.
    struct.pack(">I4s", 1, b"mdat") + b"abc",  # Incomplete extended header.
    struct.pack(">I4sQ", 1, b"mdat", 8),  # Size smaller than its header.
    struct.pack(">I4s", 4, b"mdat"),
    struct.pack(">I4s", 8, b"uuid"),  # Missing 16-byte user type.
    struct.pack(">I4sQ", 1, b"mdat", 2**33),  # Truncated >4GB box.
])
def test_invalid_box_boundaries(tmp_path, tail):
    video = tmp_path / "lecture.mp4.tmp"
    video.write_bytes(box(b"ftyp") + tail)
    with pytest.raises(IncompleteMediaError):
        check_mp4_completeness(video)


def test_reuse_reads_headers_without_reading_video_payload(tmp_path, monkeypatch):
    video = tmp_path / "lecture.mp4"
    size = 2**33
    with video.open("wb") as stream:
        stream.write(box(b"ftyp"))
        stream.write(struct.pack(">I4sQ", 1, b"mdat", size))
        stream.seek(8 + size - 1)  # Sparse fixture; no 8GB allocation.
        stream.write(b"\0")
    original_open = Path.open
    reads = []

    class HeaderReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def seek(self, position):
            return self.stream.seek(position)
        def read(self, length):
            assert 0 <= length <= 8
            reads.append(length)
            return self.stream.read(length)

    monkeypatch.setattr(Path, "open", lambda self, *a, **kw: HeaderReader(original_open(self, *a, **kw)))
    check_mp4_completeness(video)
    assert sum(reads) <= 32


def test_probe_rejects_partial_media_before_ffprobe_or_asr(tmp_path, monkeypatch):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(recording()[:-64])
    monkeypatch.setattr("src.media.run_media", lambda *a, **kw: pytest.fail("Must fail before ffprobe"))
    with pytest.raises(IncompleteMediaError):
        probe(video)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("has_length", [False, True])
def test_download_does_not_accept_truncation_even_if_http_length_matches(tmp_path, monkeypatch, legacy, has_length):
    from src.icourse import ICourseClient
    from src.pipeline import _download_video_with_progress

    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"previous recording")
    body = recording()[:-64]
    headers = {"content-type": "video/mp4"}
    if has_length:
        headers["content-length"] = str(len(body))
    response = SimpleNamespace(status_code=200, headers=headers, close=Mock(), raise_for_status=Mock(),
                               iter_content=lambda **kw: iter([body]))
    client = ICourseClient(None)
    monkeypatch.setattr(client, "get_video_response", lambda *_, **kw: response)
    with pytest.raises(IncompleteMediaError):
        if legacy:
            client.download_video("https://example.invalid/video", str(video))
        else:
            _download_video_with_progress(client, "https://example.invalid/video", video)
    assert video.read_bytes() == b"previous recording"
    assert not list(tmp_path.rglob("*.json"))
    assert not list(tmp_path.glob("*.tmp"))
    response.close.assert_called_once()


def test_retry_failed_transcription_redownloads_truncated_video(tmp_path, monkeypatch):
    import wave

    from src.asr import Segment, TranscriptionResult
    from src.icourse import ICourseClient
    from src.pipeline import main
    from src.summarizer import Summarizer
    from src.summary_result import SummaryResult
    from src.transcriber import Transcriber

    video = tmp_path / "videos" / "12345-Test" / "录屏" / "课_123456.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(recording()[:-64])
    artifact_metadata(video, kind="video")
    calls = []
    audio = io.BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000)
    complete = audio.getvalue()
    response = SimpleNamespace(status_code=200, headers={"content-length": str(len(complete))},
        close=Mock(), raise_for_status=Mock(), iter_content=lambda **kw: iter([complete]))
    monkeypatch.setattr("src.pipeline._login_with_retry", lambda *a, **kw: object())
    monkeypatch.setattr(ICourseClient, "get_course_detail", lambda *a: dict(title="Test", lectures=[
        dict(sub_id="123456", sub_title="课", has_playback=True),
        dict(sub_id="123457", sub_title="其他课", has_playback=True)]))
    monkeypatch.setattr(ICourseClient, "get_video_url", lambda *a: "https://example.invalid/video")

    def download(*_, **kw):
        calls.append("download")
        assert video.read_bytes() == recording()[:-64]
        return response

    def transcribe(self, source, **kw):
        calls.append("transcribe")
        assert Path(source).read_bytes() == complete
        settings = self.settings
        return TranscriptionResult("测试原文", (Segment(0, 1, "测试原文"),), "zh", 1, 1,
                                   settings.backend, settings.model, "test", True, 1, settings.fingerprint)

    monkeypatch.setattr(ICourseClient, "get_video_response", download)
    monkeypatch.setattr(Transcriber, "transcribe_result", transcribe)
    monkeypatch.setattr(Summarizer, "__init__", lambda self: None)

    def summarize(*a, **kw):
        calls.append("summary")
        return SummaryResult("### 完整笔记\n正文", "test")

    monkeypatch.setattr(Summarizer, "summarize", summarize)
    monkeypatch.setenv("StuId", "test-user")
    monkeypatch.setenv("UISPsw", "test-password")
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("sys.argv", ["icourse", "--mode", "download_and_summarize", "--course-ids", "12345",
        "--target", "12345:123456", "--resume-stage", "12345:123456:tr", "--sleep", "0",
        "--out-dir", str(tmp_path / "videos"), "--summary-dir", str(tmp_path / "notes")])
    assert main() == 0
    assert calls == ["download", "transcribe", "summary"]
    assert valid_artifact(video)
    assert len(list((tmp_path / "notes").rglob("*.md"))) == 1
