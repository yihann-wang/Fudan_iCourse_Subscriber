import io
import json
import re
import socket
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from src.video_download import DownloadError, _paths, download_video, retained_bytes


def audio():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        f.writeframes(bytes(range(256)) * 128)
    return out.getvalue()


def response(body=b"", *, status=206, headers=None, error=None):
    def content(**_):
        if body:
            yield body
        if error:
            raise error
    return SimpleNamespace(status_code=status, headers=headers or {},
                           iter_content=content, close=Mock())


def interrupted(body, *, etag='"original"', total=None):
    return response(body[:512], headers={"ETag": etag,
        "Content-Range": f"bytes 0-1023/{total or len(body)}", "Content-Length": "1024"},
        error=requests.exceptions.ChunkedEncodingError("https://private.invalid/?token=secret"))


def seed(tmp_path, monkeypatch, *, etag='"original"'):
    monkeypatch.setattr("src.video_download.RANGE_BYTES", 1024)
    video = tmp_path / "lecture_123456.mp4"
    video.write_bytes(b"previous recording")
    source = audio()
    client = SimpleNamespace(get_video_response=Mock(return_value=interrupted(source, etag=etag)))
    with pytest.raises(DownloadError, match="连接中断") as error:
        download_video(client, "https://example.invalid/lesson?token=old", video)
    assert "secret" not in str(error.value)
    assert retained_bytes(video) == 512
    assert video.read_bytes() == b"previous recording"
    return video, source, client


def test_real_http_disconnect_then_resume_with_rotated_signed_url(tmp_path, monkeypatch):
    """Exercise requests streaming against an actual truncated HTTP response."""
    monkeypatch.setattr("src.video_download.RANGE_BYTES", 4096)
    source = audio()
    ranges = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers["Range"])
            start, end = map(int, match.groups())
            end = min(end, len(source) - 1)
            ranges.append((start, end, self.headers.get("If-Range")))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(source)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("ETag", '"stable"')
            self.end_headers()
            if len(ranges) == 1:
                self.wfile.write(source[:512])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
            else:
                self.wfile.write(source[start:end + 1])

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"previous recording")
    session = requests.Session()
    session.trust_env = False
    client = SimpleNamespace(get_video_response=lambda url, **kw: session.get(url, stream=True, **kw))
    url = f"http://127.0.0.1:{server.server_port}/lecture"
    try:
        with pytest.raises(DownloadError, match="连接中断"):
            download_video(client, url + "?token=private-old", video, chunk_size=128)
        assert retained_bytes(video) == 512
        assert video.read_bytes() == b"previous recording"
        part, checkpoint = _paths(video)
        assert part.parent.name == ".icourse"
        assert part.stat().st_mode & 0o777 == checkpoint.stat().st_mode & 0o777 == 0o600
        assert "private-old" not in checkpoint.read_text()
        assert "127.0.0.1" not in checkpoint.read_text()
        progress = []
        download_video(client, url + "?token=private-new", video, chunk_size=128,
                       progress=lambda *x: progress.append(x))
        assert ranges[1][0] == 512
        assert all(x[2] == '"stable"' for x in ranges[1:])
        assert sum(end - start + 1 for start, end, _ in ranges[1:]) == len(source) - 512
        assert progress[-1] == (len(source), len(source), len(source) - 512)
        assert video.read_bytes() == source
        assert not part.exists() and not checkpoint.exists()
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_200_after_if_range_replaces_instead_of_appending(tmp_path, monkeypatch):
    video, source, client = seed(tmp_path, monkeypatch)
    newer = source[:-256] + b"\x22" * 256
    result = response(newer, status=200, headers={"ETag": '"new"', "Content-Length": str(len(newer))})
    client.get_video_response = Mock(return_value=result)
    download_video(client, "https://example.invalid/lesson?token=new", video)
    assert client.get_video_response.call_args.kwargs["headers"]["If-Range"] == '"original"'
    assert video.read_bytes() == newer
    result.close.assert_called_once()


@pytest.mark.parametrize("headers", [
    {"Content-Range": "bytes 512-1023/32812", "ETag": '"changed"'},
    {"Content-Range": "bytes 512-1023/50000", "ETag": '"original"'},
])
def test_changed_representation_is_not_joined(tmp_path, monkeypatch, headers):
    video, source, client = seed(tmp_path, monkeypatch)
    client.get_video_response = Mock(return_value=response(source[512:1024], headers=headers))
    with pytest.raises(DownloadError, match="已变化"):
        download_video(client, "https://example.invalid/lesson", video)
    assert retained_bytes(video) == 0
    assert video.read_bytes() == b"previous recording"


@pytest.mark.parametrize("content_range,length", [
    ("bytes 0-511/32812", "512"),  # Server repeats bytes from the beginning.
    ("bytes 512-1023/*", "512"),
    ("bytes 512-1023/32812", "999"),
    ("bytes 512-2047/32812", "1536"),  # Beyond requested end.
    ("bytes 512-511/32812", "0"),
    ("invalid", "512"),
])
def test_invalid_range_never_changes_saved_prefix(tmp_path, monkeypatch, content_range, length):
    video, source, client = seed(tmp_path, monkeypatch)
    r = response(source[512:1024], headers={"Content-Range": content_range, "Content-Length": length})
    client.get_video_response = Mock(return_value=r)
    with pytest.raises(DownloadError):
        download_video(client, "https://example.invalid/lesson", video)
    assert _paths(video)[0].read_bytes() == source[:512]
    r.close.assert_called_once()


@pytest.mark.parametrize("etag", [None, 'W/"weak"'])
def test_no_strong_validator_falls_back_to_full_response(tmp_path, monkeypatch, etag):
    monkeypatch.setattr("src.video_download.RANGE_BYTES", 1024)
    source = audio()
    video = tmp_path / "lecture.mp4"
    h = {"Content-Range": f"bytes 0-1023/{len(source)}", "Content-Length": "1024"}
    if etag:
        h["ETag"] = etag
    first = response(source[:1024], headers=h)
    second = response(source, status=200, headers={"Content-Length": str(len(source))})
    client = SimpleNamespace(get_video_response=Mock(side_effect=[first, second]))
    download_video(client, "https://example.invalid/lecture", video)
    assert "Range" not in client.get_video_response.call_args.kwargs["headers"]
    assert video.read_bytes() == source
    first.close.assert_called_once()
    second.close.assert_called_once()


def test_full_stream_interruption_can_resume_if_strong_validator(tmp_path, monkeypatch):
    monkeypatch.setattr("src.video_download.RANGE_BYTES", 1024)
    source = audio()
    video = tmp_path / "lecture.mp4"
    r = response(source[:1024], status=200, headers={"ETag": '"v1"', "Content-Length": str(len(source))})
    client = SimpleNamespace(get_video_response=Mock(return_value=r))
    with pytest.raises(DownloadError, match="传输中断"):
        download_video(client, "https://example.invalid/lesson", video)
    assert retained_bytes(video) == 1024
    assert not video.exists()


@pytest.mark.parametrize("bad_headers", [
    {"Content-Type": "text/html"}, {"Content-Encoding": "gzip"}, {"Content-Length": "n/a"},
])
def test_bad_response_rejected_and_closed(tmp_path, bad_headers):
    r = response(b"not video", status=200, headers=bad_headers)
    client = SimpleNamespace(get_video_response=Mock(return_value=r))
    with pytest.raises(DownloadError):
        download_video(client, "https://example.invalid/lesson", tmp_path / "lecture.mp4")
    assert not list(tmp_path.rglob("*.part"))
    r.close.assert_called_once()


def test_changed_total_in_416_discards_stale_binding(tmp_path, monkeypatch):
    video, _, client = seed(tmp_path, monkeypatch)
    client.get_video_response = Mock(return_value=response(status=416, headers={"Content-Range": "bytes */100"}))
    with pytest.raises(DownloadError, match="大小已变化"):
        download_video(client, "https://example.invalid/lesson", video)
    assert retained_bytes(video) == 0
    assert video.read_bytes() == b"previous recording"


def test_keyboard_interrupt_retains_checkpoint_and_closes_response(tmp_path, monkeypatch):
    monkeypatch.setattr("src.video_download.RANGE_BYTES", 1024)
    source = audio()
    video = tmp_path / "lecture.mp4"
    r = interrupted(source)
    def content(**_):
        yield source[:512]
        raise KeyboardInterrupt()
    r.iter_content = content
    client = SimpleNamespace(get_video_response=Mock(return_value=r))
    with pytest.raises(KeyboardInterrupt):
        download_video(client, "https://example.invalid/lesson", video)
    assert retained_bytes(video) == 512
    r.close.assert_called_once()


@pytest.mark.parametrize("delta,resumable", [(120, True), (30, False)])
def test_date_validator_requires_strong_last_modified(tmp_path, monkeypatch, delta, resumable):
    from email.utils import formatdate
    monkeypatch.setattr("src.video_download.RANGE_BYTES", 1024)
    source = audio()
    video = tmp_path / "lecture.mp4"
    h = {"Content-Length": str(len(source)), "Date": formatdate(1_800_000_000, usegmt=True),
         "Last-Modified": formatdate(1_800_000_000 - delta, usegmt=True)}
    client = SimpleNamespace(get_video_response=Mock(return_value=response(source[:512], status=200, headers=h)))
    with pytest.raises(DownloadError):
        download_video(client, "https://example.invalid/lesson", video)
    assert bool(retained_bytes(video)) == resumable
    if resumable:
        saved = json.loads(_paths(video)[1].read_text())
        assert saved["validator"] == ["last-modified", h["Last-Modified"]]


def test_unavailable_media_tool_retains_finished_download(tmp_path, monkeypatch):
    source = audio()
    video = tmp_path / "lecture.mp4"
    r = response(source, status=200, headers={"Content-Length": str(len(source)), "ETag": '"v1"'})
    client = SimpleNamespace(get_video_response=Mock(return_value=r))
    from src.media import probe
    monkeypatch.setattr("src.media.probe", Mock(side_effect=FileNotFoundError("ffprobe")))
    with pytest.raises(FileNotFoundError):
        download_video(client, "https://example.invalid/lesson", video)
    assert retained_bytes(video) == len(source)
    monkeypatch.setattr("src.media.probe", probe)
    download_video(client, "https://example.invalid/lesson", video)
    client.get_video_response.assert_called_once()
    assert video.read_bytes() == source


def test_pipeline_retry_refreshes_url_and_resumes_without_repeating_bytes(tmp_path, monkeypatch, capsys):
    import queue
    from src.pipeline import _Counters, _download_stage
    from src.pipeline_state import valid_artifact

    monkeypatch.setattr("src.video_download.RANGE_BYTES", 1024)
    monkeypatch.setattr("src.pipeline.time.sleep", lambda _: None)
    source = audio()
    video = tmp_path / "lesson.mp4"
    calls = []
    def get(url, **kw):
        calls.append((url, kw["headers"]))
        if len(calls) == 1:
            return interrupted(source)
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", kw["headers"]["Range"]).groups())
        end = min(end, len(source) - 1)
        return response(source[start:end + 1], headers={
            "Content-Range": f"bytes {start}-{end}/{len(source)}", "ETag": '"original"'})
    client = SimpleNamespace(get_video_url=Mock(side_effect=["https://example.invalid/lesson?t=old",
        "https://example.invalid/lesson?t=new"]), get_video_response=get, check_alive=Mock(return_value=True))
    incoming, outgoing = queue.Queue(), queue.Queue()
    incoming.put(dict(course_id="12345", sub_id="123456", target_video_path=video))
    incoming.put(None)
    counters = _Counters()
    _download_stage(incoming, outgoing, client, 0, counters)
    assert counters.downloaded == 1 and counters.failed == 0
    assert calls[1][1]["Range"].startswith("bytes=512-")
    assert all(url.endswith("t=new") for url, _ in calls[1:])
    assert video.read_bytes() == source and valid_artifact(video)
    assert outgoing.get_nowait()["video_path"] == video
    assert outgoing.get_nowait() is None
    logs = capsys.readouterr().out
    assert "连接中断" in logs and "已保留" in logs
    assert "example.invalid" not in logs and "private.invalid" not in logs
