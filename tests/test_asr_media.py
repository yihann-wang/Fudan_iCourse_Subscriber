import threading
import wave
from dataclasses import replace

import pytest

from src.asr import ASRSettings
from src.asr.types import MLX_MODEL, MLX_REVISION
from src.asr.worker import merge_chunk_segments
from src.media import CancelledError, IncompleteAudioError, decode, probe, resolve_media_tool, run_media
from src.transcriber import Transcriber, write_srt


@pytest.fixture
def apple(monkeypatch):
    monkeypatch.setattr("src.asr.types.platform.system", lambda: "Darwin")
    monkeypatch.setattr("src.asr.types.platform.machine", lambda: "arm64")


def test_old_cuda_config_selects_mlx_on_mac(apple):
    s = ASRSettings.from_env({"WHISPER_DEVICE": "cuda", "WHISPER_COMPUTE_TYPE": "int8_float16"})
    assert (s.backend, s.model, s.revision, s.compute_type) == ("mlx", MLX_MODEL, MLX_REVISION, "float16")


def test_cpu_uses_supported_precision(apple):
    assert ASRSettings.from_env({"ASR_BACKEND": "cpu", "WHISPER_COMPUTE_TYPE": "int8_float16"}).compute_type == "int8"


@pytest.mark.parametrize("backend", ["mps", "cuda"])
def test_invalid_mac_device_fails_early(apple, backend):
    with pytest.raises(ValueError):
        ASRSettings(backend=backend).resolved()


def test_settings_fingerprint_changes_with_decoding_options(apple):
    a = ASRSettings().resolved()
    assert a.fingerprint != replace(a, language="en").fingerprint
    assert a.fingerprint != replace(a, chunk_seconds=60).fingerprint


def test_chunk_overlap_assigns_one_owner():
    segments = [dict(start=57, end=59, text="前句"), dict(start=59, end=61, text="边界"),
                dict(start=61, end=63, text="后句")]
    first = merge_chunk_segments(segments, read_start=0, core_start=0, core_end=60, duration=120)
    second = merge_chunk_segments([dict(start=s["start"] - 58, end=s["end"] - 58, text=s["text"]) for s in segments],
                                  read_start=58, core_start=60, core_end=120, duration=120)
    assert [s["text"] for s in first + second] == ["前句", "边界", "后句"]


def test_srt_timestamps_are_valid_and_atomic(tmp_path):
    p = tmp_path / "课程.srt"
    assert write_srt([(-1, 1, "第一句"), (0.5, 1.5, "第二句"), (2, 2, "第三句")], p) == 3
    text = p.read_text()
    assert "00:00:00,000 --> 00:00:01,000" in text
    assert "00:00:01,000 --> 00:00:01,500" in text
    assert not list(tmp_path.glob("*.part"))


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "audio.wav"
    with wave.open(str(p), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000)
    return p


def test_decode_checks_real_pcm_duration(wav, tmp_path):
    result = decode([resolve_media_tool("ffmpeg"), "-i", str(wav)], tmp_path / "decoded.wav")
    assert result.duration == pytest.approx(1)
    assert probe(wav)["has_audio"]
    with pytest.raises(IncompleteAudioError):
        decode([resolve_media_tool("ffmpeg"), "-i", str(wav)], tmp_path / "partial.wav", source_duration=120)


def test_media_cancel_terminates_child():
    import sys
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(CancelledError):
        run_media([sys.executable, "-c", "import time; time.sleep(60)"], timeout=10, cancel=cancel)


def test_failed_attempt_does_not_reuse_previous_segments(monkeypatch, wav, tmp_path):
    transcriber = Transcriber(ASRSettings(backend="cpu"))
    monkeypatch.setattr(transcriber._worker, "request", lambda *a, **k: dict(
        segments=[dict(start=0, end=1, text="第一节课")], language="zh", revision="test"))
    assert transcriber.transcribe_video(wav) == "第一节课"
    with pytest.raises(RuntimeError):
        transcriber.transcribe_video(tmp_path / "missing.mp4")
    assert transcriber.last_segments == []
    assert transcriber.last_result is None
    transcriber.close()


def test_cli_cache_checks_both_artifacts(monkeypatch, tmp_path):
    from src.cli import main
    from src.asr import Segment, TranscriptionResult
    media = tmp_path / "lecture.wav"
    media.write_bytes(b"source")
    settings = ASRSettings.from_env()
    calls = []

    def result(self, *_args, **_kwargs):
        calls.append(1)
        self._last_segments = [(0, 1, "测试")]
        return TranscriptionResult("测试", (Segment(0, 1, "测试"),), "zh", 1, 1,
                                   settings.backend, settings.model, "test", True, 1, settings.fingerprint)

    monkeypatch.setattr(Transcriber, "transcribe_result", result)
    args = ["transcribe", str(media), "--output-dir", str(tmp_path / "out")]
    assert main(args) == 0
    assert main(args) == 0
    assert len(calls) == 1
    next((tmp_path / "out").glob("*.srt")).write_text("corrupt")
    assert main(args) == 0
    assert len(calls) == 2
