import json
import threading
import time
import wave
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from src.asr import ASRSettings
from src.asr.client import CloudWorker
from src.asr.cloud import CloudAPI, SpeechAPIError, parse_response
from src.media import CancelledError
from src.transcriber import Transcriber, write_srt


@pytest.fixture
def server():
    state = dict(requests=[], responses=[], delay=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.do_POST()

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["requests"].append((self.path, self.headers, body))
            status, payload = state["responses"].pop(0) if state["responses"] else (200, {"text": "识别文字"})
            time.sleep(state["delay"])
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{http.server_port}/custom/v1"
    yield state
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


def test_arbitrary_model_address_and_optional_fields(server, tmp_path):
    key = uuid4().hex
    settings = ASRSettings(base_url=server["url"] + "/audio/transcriptions", model="another/model",
                           api_key=key, language="zh", initial_prompt="Kubernetes", response_format="verbose_json")
    api = CloudAPI(settings)
    media = tmp_path / "private-course-name.mp3"
    media.write_bytes(b"test-audio")
    try:
        assert api.transcribe(media, 1)["text"] == "识别文字"
    finally:
        api.close()
    path, headers, body = server["requests"][0]
    assert path == "/custom/v1/audio/transcriptions"
    assert headers["Authorization"] == "Bearer " + key
    assert all(value in body for value in (b"another/model", b"Kubernetes", b"verbose_json", b"test-audio"))
    assert media.name.encode() not in body
    assert key not in repr(settings) and key not in json.dumps(settings.public_dict())


@pytest.mark.parametrize("status,retry,count", [(401, False, 1), (413, False, 1), (429, True, 2), (503, True, 2)])
def test_bounded_retry_rewinds_upload_and_redacts_errors(server, tmp_path, monkeypatch, status, retry, count):
    monkeypatch.setattr("src.asr.cloud.time.sleep", lambda _: None)
    secret = uuid4().hex
    server["responses"] = [(status, {"error": secret}), (200, {"text": "完成"})]
    media = tmp_path / "a.mp3"
    media.write_bytes(b"complete-upload")
    api = CloudAPI(ASRSettings(base_url=server["url"], api_key=secret, retries=1))
    try:
        if retry:
            assert api.transcribe(media, 1)["text"] == "完成"
        else:
            with pytest.raises(SpeechAPIError) as error:
                api.transcribe(media, 1)
            assert secret not in str(error.value)
    finally:
        api.close()
    assert len(server["requests"]) == count
    assert all(b"complete-upload" in r[2] for r in server["requests"])


def test_connection_check_uploads_no_audio(server):
    server["responses"] = [(200, {"data": [{"id": "arbitrary"}]})]
    worker = CloudWorker(ASRSettings(base_url=server["url"], api_key=uuid4().hex, model="arbitrary"))
    try:
        assert "未上传音频" in worker.request()["message"]
    finally:
        worker.close()
    assert server["requests"][0][0].endswith("/models")
    assert server["requests"][0][2] == b""


def test_cancel_stops_an_in_flight_cloud_request(server, tmp_path):
    server["delay"] = 2
    media = tmp_path / "a.mp3"
    media.write_bytes(b"test")
    worker = CloudWorker(ASRSettings(base_url=server["url"], api_key=uuid4().hex))
    cancel = threading.Event()
    timer = threading.Timer(.4, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CancelledError):
            worker.request(media, duration=1, cancel=cancel)
        assert worker.process is None
        assert time.monotonic() - started < 2
    finally:
        worker.close()
        timer.cancel()


@pytest.mark.parametrize("payload", [
    {"error": "bad"}, {"text": 23}, {"text": "a", "duration": 5},
    {"text": "a", "segments": [{"start": float("nan"), "end": 1, "text": "a"}]},
    {"text": "a", "segments": [{"start": 1, "end": 999, "text": "a"}]},
])
def test_invalid_or_truncated_response_is_not_success(payload):
    with pytest.raises(SpeechAPIError):
        parse_response(payload, 30)


def test_response_formats_and_real_timing(tmp_path):
    assert parse_response({"text": "纯文本"}, 1)["segments"] == []
    timed = parse_response({"text": "原文", "segments": [{"start": 0, "end": 1, "text": "原文"}]}, 1)
    assert timed["segments"][0]["end"] == 1
    assert parse_response("1\n00:00:00,000 --> 00:00:01,000\n字幕\n", 1, "srt")["text"] == "字幕"
    assert parse_response("text", 1, "text")["text"] == "text"
    path = tmp_path / "a.srt"
    path.write_text("old timed subtitles")
    assert write_srt([], path) == 0 and not path.exists()
    assert next((tmp_path / ".icourse").glob("*.srt")).read_text() == "old timed subtitles"


def recording(path, seconds=65, sample=b"\x10\x10"):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(sample * (16000 * seconds))


def test_partial_resume_changed_model_corruption_and_timing(tmp_path, monkeypatch):
    media = tmp_path / "lecture.wav"
    recording(media)
    settings = ASRSettings(chunk_seconds=30)
    calls = []
    transcriber = Transcriber(settings, cache_dir=tmp_path / "cache")

    def request(*args, duration, **kwargs):
        calls.append(duration)
        if len(calls) == 2:
            raise RuntimeError("temporary failure")
        return dict(text=f"第 {len(calls)} 块", segments=[dict(start=0, end=duration, text="语音")], language="zh")

    monkeypatch.setattr(transcriber._worker, "request", request)
    with pytest.raises(RuntimeError, match="temporary failure"):
        transcriber.transcribe_result(media)
    assert transcriber.last_result is None
    result = transcriber.transcribe_result(media)
    assert calls == [30, 30, 30, 5]  # First chunk was not uploaded twice.
    assert result.complete and result.segments[-1].end == pytest.approx(65)
    assert result.segments[1].start == pytest.approx(30)
    assert transcriber.transcribe_result(media).text == result.text
    assert len(calls) == 4
    checkpoint = next((tmp_path / "cache").rglob("0-480000.json"))
    checkpoint.write_text("broken cache")
    transcriber.transcribe_result(media)
    assert len(calls) == 5
    transcriber.settings = replace(settings, model="another/model")
    transcriber.transcribe_result(media)
    assert len(calls) == 8
    recording(media, sample=b"\x20\x20")
    transcriber.transcribe_result(media)
    assert len(calls) == 11
    transcriber.close()


def test_text_only_lecture_is_complete_but_has_no_fake_subtitles(tmp_path, monkeypatch):
    media = tmp_path / "lecture.wav"
    recording(media, seconds=31)
    transcriber = Transcriber(ASRSettings(chunk_seconds=30), cache_dir=tmp_path / "cache")
    calls = []

    def request(*args, **kwargs):
        calls.append(1)
        return dict(text="课程原文", segments=[], language="")

    monkeypatch.setattr(transcriber._worker, "request", request)
    result = transcriber.transcribe_result(media)
    assert result.complete and result.text == "课程原文\n课程原文"
    assert result.segments == () and result.warnings
    assert len(calls) == 2
    assert transcriber.write_srt(tmp_path / "lecture.srt") == 0
    assert not (tmp_path / "lecture.srt").exists()
    transcriber.close()


def test_silence_and_empty_service_response_are_not_completed_lectures(tmp_path, monkeypatch):
    media = tmp_path / "lecture.wav"
    recording(media, seconds=1, sample=b"\0\0")
    transcriber = Transcriber(ASRSettings(), cache_dir=tmp_path / "cache")
    monkeypatch.setattr(transcriber._worker, "request", lambda *a, **k: pytest.fail("Silence was uploaded"))
    with pytest.raises(RuntimeError, match="未识别到语音"):
        transcriber.transcribe_result(media)
    recording(media, seconds=1)
    monkeypatch.setattr(transcriber._worker, "request", lambda *a, **k: dict(text="", segments=[], language=""))
    with pytest.raises(RuntimeError, match="未识别到语音"):
        transcriber.transcribe_result(media)
    assert transcriber.last_result is None
    assert not list((tmp_path / "cache").rglob("*.json"))
    monkeypatch.setattr(transcriber._worker, "request", lambda *a, **k: dict(text="服务恢复后识别成功"))
    assert transcriber.transcribe_result(media).text == "服务恢复后识别成功"
    transcriber.close()


@pytest.mark.parametrize("url", ["https://user:password@example.invalid/v1", "https://example.invalid?key=secret",
                                 "http://example.invalid", "file:///tmp/audio"])
def test_unsafe_service_addresses_are_rejected_without_echo(url):
    with pytest.raises(ValueError) as exc:
        ASRSettings(base_url=url).resolved()
    assert url not in str(exc.value)


def test_empty_opening_continues_and_is_visible_and_cached(tmp_path, monkeypatch):
    media = tmp_path / 'lecture.wav'
    recording(media, seconds=65)
    transcriber = Transcriber(ASRSettings(chunk_seconds=30), cache_dir=tmp_path / 'cache')
    replies = iter([dict(text=''), dict(text='课堂正式开始'), dict(text='最后的内容')])
    calls = []
    def request(*args, **kwargs):
        calls.append(1)
        return next(replies)
    monkeypatch.setattr(transcriber._worker, 'request', request)
    result = transcriber.transcribe_result(media)
    assert result.complete and '课堂正式开始' in result.text and '最后的内容' in result.text
    assert '00:00:00,000–00:00:30,000 未识别到文字' in result.text
    assert any('1 段音频未识别到文字' in w for w in result.warnings)
    assert transcriber.transcribe_result(media).text == result.text
    assert len(calls) == 3
    transcriber.close()
