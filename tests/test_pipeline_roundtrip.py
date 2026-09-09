import io
import sqlite3
import wave
import pytest

from src.asr import ASRSettings, Segment, TranscriptionResult
from src.pipeline import main
from src.summary_result import SummaryResult


def test_all_stages_same_title_and_cached_rerun(monkeypatch, tmp_path):
    from src.icourse import ICourseClient
    from src.summarizer import Summarizer
    from src.transcriber import Transcriber

    audio = io.BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000)
    calls = {"download": 0, "asr": 0, "summary": 0}

    class Response:
        headers = {"content-type": "video/mp4", "content-length": str(len(audio.getvalue()))}
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            yield audio.getvalue()
        def close(self):
            pass

    class VPN:
        def get(self, *_, **kwargs):
            from src import config
            from src.webvpn import get_vpn_url
            # The real replay server returns 403 without its player referrer.
            assert kwargs["headers"]["Referer"] == get_vpn_url(config.ICOURSE_BASE + "/")
            calls["download"] += 1
            return Response()

    monkeypatch.setattr("src.pipeline._login_with_retry", lambda *a, **k: VPN())
    monkeypatch.setattr(ICourseClient, "get_course_detail", lambda *a: {
        "title": "课程", "lectures": [dict(sub_id=id, sub_title="相同标题", has_playback=True)
                                       for id in ("123456", "123457")]})
    monkeypatch.setattr(ICourseClient, "get_video_url", lambda *a: "https://example.invalid/video")
    monkeypatch.setattr(Summarizer, "__init__", lambda self: None)

    def summarize(*_, **kwargs):
        assert kwargs["checkpoint_path"].parent.name == ".icourse"
        kwargs["progress"]("第 1/1 段 · 生成中")
        calls["summary"] += 1
        return SummaryResult("### 知识点\n\n这是一条测试笔记。", "mock-model")

    def transcribe(self, *_args, **_kwargs):
        calls["asr"] += 1
        self._last_segments = [(0, 1, "测试内容")]
        settings = ASRSettings.from_env()
        return TranscriptionResult("测试内容", (Segment(0, 1, "测试内容"),), "zh", 1, 1,
                                   settings.backend, settings.model, "test", True, 1, settings.fingerprint)

    monkeypatch.setattr(Summarizer, "summarize", summarize)
    monkeypatch.setattr(Transcriber, "transcribe_result", transcribe)
    monkeypatch.setenv("StuId", "test-user")
    monkeypatch.setenv("UISPsw", "test-password")
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("sys.argv", ["icourse", "--mode", "download_and_summarize", "--course-ids", "12345",
                                    "--out-dir", str(tmp_path / "videos"), "--summary-dir", str(tmp_path / "notes"), "--sleep", "0"])
    assert main() == 0
    assert calls == {"download": 2, "asr": 2, "summary": 2}
    assert len(list((tmp_path / "videos").rglob("*.mp4"))) == 2
    assert len(list((tmp_path / "videos").rglob("*.srt"))) == 2
    assert len(list((tmp_path / "notes").rglob("*.md"))) == 2
    assert all(p.parent.name == ".icourse" for p in tmp_path.rglob("*.json"))
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "16384")
    monkeypatch.setenv("LLM_INPUT_CHAR_LIMIT", "4000")
    assert main() == 0
    assert calls == {"download": 2, "asr": 2, "summary": 2}
    connection = sqlite3.connect(tmp_path / "state" / "pipeline.sqlite3")
    assert connection.execute("SELECT count(*) FROM pipeline_jobs WHERE sub_id IS NOT NULL AND status='done'").fetchone()[0] == 6
    connection.close()


def test_owning_run_lock_rejects_second_pipeline(monkeypatch, tmp_path):
    from filelock import FileLock
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path))
    with FileLock(tmp_path / "pipeline.lock"):
        assert main() == 2


@pytest.mark.parametrize("mode", ["summarize", "download_and_summarize"])
@pytest.mark.parametrize("has_transcript", [True, False])
def test_redo_notes_reuses_transcript_and_never_downloads_or_transcribes(mode, has_transcript, monkeypatch, tmp_path):
    from src.icourse import ICourseClient
    from src.summarizer import Summarizer
    from src.transcriber import Transcriber
    video = tmp_path / "videos" / "12345-Test" / "录屏" / "课_123456.mp4"
    transcript = tmp_path / "notes" / "12345-Test" / "原始txt" / "课_123456.txt"
    notes = tmp_path / "notes" / "12345-Test" / "笔记" / "课_123456.md"
    for p in (video, transcript, notes):
        p.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"legacy video")
    if has_transcript:
        transcript.write_text("原有完整转录")
    notes.write_text("原有笔记")
    monkeypatch.setattr(Summarizer, "__init__", lambda self: None)
    calls = []
    def summarize(self, title, content, **kwargs):
        assert content == "原有完整转录" and kwargs["overwrite"] is False
        calls.append(content)
        return SummaryResult("### 新的整课笔记\n完整正文", "test")
    monkeypatch.setattr(Summarizer, "summarize", summarize)
    monkeypatch.setattr(Transcriber, "transcribe_result", lambda *a, **k: pytest.fail("Unexpected transcription"))
    monkeypatch.setattr(ICourseClient, "get_video_url", lambda *a, **k: pytest.fail("Unexpected download"))
    monkeypatch.setattr("src.pipeline._login_with_retry", lambda *a, **k: object())
    monkeypatch.setattr(ICourseClient, "get_course_detail", lambda *a, **k: dict(title="Test", lectures=[
        dict(sub_id="123456", sub_title="课", has_playback=True)]))
    monkeypatch.setenv("StuId", "test-user")
    monkeypatch.setenv("UISPsw", "test-password")
    monkeypatch.setenv("ICOURSE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("sys.argv", ["icourse", "--mode", mode, "--course-ids", "12345", "--redo-notes",
        "--out-dir", str(tmp_path / "videos"), "--summary-dir", str(tmp_path / "notes"), "--sleep", "0"])
    expected_code = 0 if has_transcript else 1
    assert main() == expected_code
    if has_transcript:
        assert len(calls) == 1
        assert "新的整课笔记" in notes.read_text()
        assert not (notes.parent / "待核对").exists()
        assert transcript.read_text() == "原有完整转录"
    else:
        assert calls == [] and notes.read_text() == "原有笔记" and not transcript.exists()
