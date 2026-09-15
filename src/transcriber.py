"""Compatible public transcription API over isolated MLX/CPU/CUDA backends."""

import os
import tempfile
import time
from pathlib import Path

from . import task_events as events
from .artifacts import atomic_write_text
from .asr import ASRSettings, Segment, TranscriptionResult
from .asr.client import ASRWorker
from .media import IncompleteAudioError as IncompleteAudioError
from .media import (
    NoAudioStreamError,
    decode,
    probe,
    resolve_media_tool,
    windows_subprocess_kwargs,
)


def _bar(pct: float, width: int = 18) -> str:
    """pip/wget-style ASCII progress bar: '=======>          ' for 0..100.

    The '>' head stays visible until exactly 100% (floor, not round).
    """
    pct = max(0.0, min(100.0, float(pct)))
    if pct >= 100.0:
        return "=" * width
    if pct <= 0.0:
        return " " * width
    filled = int(pct / 100.0 * width)  # floor → never fully solid before 100%
    return "=" * filled + ">" + " " * (width - filled - 1)


# Mirrors downloader._prog: the GUI sets ICOURSE_GUI=1 and interprets the
# [PROG:<key>] prefix as a refreshable line. Transcription runs in the same
# process as the downloader, so its progress refreshes the SAME tr:<sub_id>
# line that the transcribe stage created.
_GUI_MODE = os.environ.get("ICOURSE_GUI") == "1"


def _prog(prog_key: str | None, message: str) -> None:
    """Refreshable progress line (GUI) or timestamped append (CLI)."""
    if events.enabled():
        return
    if _GUI_MODE and prog_key:
        print(f"[PROG:{prog_key}] {message}", flush=True)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def _srt_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp 'HH:MM:SS,mmm'."""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list[tuple[float, float, str]], path) -> int:
    """Write timed (start, end, text) segments as an SRT subtitle file.

    Players (VLC / PotPlayer / mpv) auto-load a .srt sitting next to the video
    with the same basename. Returns the number of entries written.
    """
    from pathlib import Path as _Path

    path = _Path(path)
    lines: list[str] = []
    n = 0
    prev_end = 0.0
    for start, end, text in segments:
        text = (text or "").strip()
        if not text:
            continue
        # Guard against zero/negative-length or overlapping cues.
        start = max(float(start), prev_end)
        if end <= start:
            end = start + 0.5
        prev_end = end
        n += 1
        lines.append(str(n))
        lines.append(f"{_srt_timestamp(start)} --> {_srt_timestamp(end)}")
        lines.append(text)
        lines.append("")  # blank line between cues
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, "\n".join(lines))
    return n


class Transcriber:
    """One persistent model worker per instance; close after a batch finishes.

    Existing string-returning methods remain available. New consumers should
    use transcribe_result(), whose metadata belongs to that result explicitly.
    """

    def __init__(self, settings: ASRSettings | None = None, cancel=None):
        self.settings = (settings or ASRSettings.from_env()).resolved()
        self.cancel = cancel
        self._worker = ASRWorker(self.settings)
        self.last_result = None
        self._last_duration = 0.0
        self._last_transcript = ""
        self._last_segments = []
        self._media_duration = None

    _resolve_media_tool = staticmethod(resolve_media_tool)
    _windows_subprocess_kwargs = staticmethod(windows_subprocess_kwargs)

    @property
    def last_segments(self):
        return list(self._last_segments)

    def write_srt(self, path):
        return write_srt(self.last_segments, path)

    def close(self):
        self._worker.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _transcribe(self, input_only_cmd, timeout=7200, prog_key=None,
                    title="", tag="", source_duration=None):
        # A failed attempt must never expose an earlier lecture's segments.
        self.last_result = None
        self._last_duration = 0.0
        self._last_transcript = ""
        self._last_segments = []
        self._media_duration = None
        head = f"tr  {title} {tag}".rstrip()
        started = time.monotonic()

        def report(event):
            if event["event"] == "loading":
                events.progress("正在加载转录模型", phase="loading", backend=event.get("backend"), model=event.get("model"))
                _prog(prog_key, f"{head} · 加载 {event['backend']} 模型…")
            elif event["event"] == "ready":
                events.progress("模型就绪，准备转录", phase="ready")
                _prog(prog_key, f"{head} · 模型就绪，开始转录…")
            elif event["event"] == "progress":
                events.progress("正在转录", phase="transcribing", unit="seconds", completed=event["completed"],
                                total=event["total"], backend=self.settings.backend)
                pct = event["completed"] / event["total"] * 100
                _prog(prog_key, f"{head} [{_bar(pct)}] {pct:.1f}% · "
                      f"{event['completed']:.0f}/{event['total']:.0f}s")

        with tempfile.TemporaryDirectory(prefix="icourse-audio-", dir=os.environ.get("ICOURSE_RUN_TEMP")) as tmp:
            events.progress("正在准备音频", phase="decoding")
            _prog(prog_key, f"{head} · 解码音频…")
            audio = decode(input_only_cmd, Path(tmp) / "audio.wav", timeout=timeout,
                           cancel=self.cancel, source_duration=source_duration)
            payload = self._worker.request(audio.path, cancel=self.cancel, progress=report)
        segments = tuple(Segment(**s) for s in payload["segments"])
        text = " ".join(s.text for s in segments).strip()
        warnings = () if audio.source_duration else ("源时长未知，无法核对完整性。",)
        result = TranscriptionResult(
            text=text, segments=segments, language=payload["language"],
            decoded_duration=audio.duration, source_duration=audio.source_duration,
            backend=self.settings.backend, model=self.settings.model,
            model_revision=payload["revision"], complete=audio.source_duration is not None,
            elapsed_seconds=time.monotonic() - started,
            settings_fingerprint=self.settings.fingerprint, warnings=warnings,
            inference_seconds=payload.get("inference_seconds", 0),
            peak_memory_bytes=payload.get("peak_memory_bytes"),
        )
        self.last_result = result
        self._last_transcript = result.text
        self._last_segments = [(s.start, s.end, s.text) for s in segments]
        self._last_duration = result.decoded_duration
        self._media_duration = result.source_duration
        speed = audio.duration / max(0.001, result.elapsed_seconds)
        _prog(prog_key, f"{head} · 完成 {len(text)} 字 · {speed:.1f} 倍实时速度 · "
              f"耗时 {result.elapsed_seconds:.1f}s")
        return text

    def transcribe_result(self, video_path, prog_key=None, title="", tag=""):
        self.last_result = None
        self._last_segments = []
        self._last_transcript = ""
        self._last_duration = 0.0
        self._media_duration = None
        info = probe(video_path, cancel=self.cancel)
        if not info["has_audio"]:
            raise NoAudioStreamError("录像不含音频轨道。")
        self._transcribe([resolve_media_tool("ffmpeg"), "-i", str(video_path)],
                         prog_key=prog_key, title=title, tag=tag,
                         source_duration=info["duration"])
        return self.last_result

    def transcribe_video(self, video_path, prog_key=None, title="", tag=""):
        return self.transcribe_result(video_path, prog_key, title, tag).text

    def transcribe_url(self, url, timeout=7200, http_headers=None):
        cmd = [resolve_media_tool("ffmpeg")]
        if http_headers:
            cmd += ["-headers", http_headers]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5", "-i", url]
        return self._transcribe(cmd, timeout=timeout)

    @staticmethod
    def probe_duration(url, http_headers=None, timeout=30):
        try:
            return probe(url, headers=http_headers, timeout=timeout)["duration"]
        except (OSError, RuntimeError, TimeoutError, ValueError):
            return None
