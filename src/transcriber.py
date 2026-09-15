"""Cloud transcription with resumable audio chunks and optional real timestamps."""

import hashlib
import json
import os
import wave
from array import array
import tempfile
import time
from pathlib import Path

from . import task_events as events
from filelock import FileLock, Timeout as LockTimeout
from platformdirs import user_data_path

from .artifacts import atomic_write_json, atomic_write_text, cached_file_sha256, file_sha256, internal_path
from .asr import ASRSettings, Segment, TranscriptionResult
from .asr.client import CloudWorker
from .asr.cloud import parse_response
from .media import IncompleteAudioError as IncompleteAudioError
from .media import (
    CancelledError,
    NoAudioStreamError,
    decode,
    probe,
    resolve_media_tool,
    run_media,
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
    if not segments:
        # Preserve old local-model subtitles without leaving a stale active SRT.
        if path.is_file():
            backup = internal_path(path.with_name(path.stem + ".previous-" + file_sha256(path)[:12] + ".srt"))
            backup.parent.mkdir(parents=True, exist_ok=True)
            path.replace(backup)
        return 0
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
    """A lightweight network worker per instance; close after a batch finishes.

    Existing string-returning methods remain available. New consumers should
    use transcribe_result(), whose metadata belongs to that result explicitly.
    """

    def __init__(self, settings: ASRSettings | None = None, cancel=None, cache_dir=None):
        self.settings = (settings or ASRSettings.from_env()).resolved()
        self.cancel = cancel
        self._worker = CloudWorker(self.settings)
        self.cache_dir = Path(cache_dir) if cache_dir else user_data_path(
            "Fudan iCourse Subscriber", appauthor=False) / "asr-cache"
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

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise CancelledError("转录已取消。")

    @staticmethod
    def _chunk_end(wav, start, limit, frames):
        end = min(start + limit, frames)
        if end == frames:
            return end
        # Prefer a quiet 200 ms boundary in the last 8 seconds. No words are
        # removed: adjacent chunks cover exactly the original sample range.
        rate = wav.getframerate()
        search = max(start, end - 8 * rate)
        wav.setpos(search)
        samples = array("h", wav.readframes(end - search))
        import sys
        if sys.byteorder != "little":
            samples.byteswap()
        width = rate // 5
        candidates = []
        for offset in range(0, len(samples) - width + 1, width):
            energy = sum(v*v for v in samples[offset:offset+width]) / width
            if energy < 250**2:
                candidates.append(search + offset + width // 2)
        return candidates[-1] if candidates else end

    def _chunks(self, audio, cache, report):
        texts, segments, languages = [], [], []
        all_timed = True
        # MP3 at 64 kbps, with headroom for ID3 and encoding padding.
        seconds = min(self.settings.chunk_seconds, (self.settings.max_upload_mb * 1000000 - 65536) // 8000)
        with wave.open(str(audio.path), "rb") as wav:
            rate, frames = wav.getframerate(), wav.getnframes()
            start, index = 0, 0
            while start < frames:
                self._check_cancel()
                end = self._chunk_end(wav, start, seconds * rate, frames)
                duration = (end - start) / rate
                index += 1
                checkpoint = cache / f"{start}-{end}.json"
                payload = None
                if checkpoint.is_file():
                    try:
                        saved = json.loads(checkpoint.read_text())
                        raw = json.dumps(saved["result"], sort_keys=True, ensure_ascii=False).encode()
                        if saved["sha256"] == hashlib.sha256(raw).hexdigest():
                            payload = parse_response(saved["result"], duration)
                    except (ValueError, KeyError, TypeError, RuntimeError):
                        pass
                report(start/rate, audio.duration, f"云端转录 · 音频块 {index} · " +
                       ("复用已完成结果" if payload is not None else "正在准备上传"))
                if payload is None:
                    wav.setpos(start)
                    pcm = wav.readframes(end-start)
                    if len(pcm) != (end-start) * 2:
                        raise RuntimeError("音频块不完整，已停止转录。")
                    if not any(pcm):
                        payload = dict(text="", segments=[], language="")
                    else:
                        part = audio.path.parent / "part.wav"
                        upload = audio.path.parent / "part.mp3"
                        with wave.open(str(part), "wb") as dest:
                            dest.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                            dest.writeframes(pcm)
                        del pcm
                        code, _, _ = run_media([resolve_media_tool("ffmpeg"), "-nostdin", "-v", "error", "-y",
                            "-i", str(part), "-c:a", "libmp3lame", "-b:a", "64k", str(upload)],
                            timeout=300, cancel=self.cancel)
                        part.unlink(missing_ok=True)
                        if code or not upload.is_file():
                            raise RuntimeError("上传音频准备失败。")
                        if upload.stat().st_size > self.settings.max_upload_mb * 1000000:
                            raise RuntimeError("音频块超出上传限制，请减小音频块时长。")
                        report(start/rate, audio.duration, f"云端转录 · 音频块 {index} · 等待语音服务返回")
                        try:
                            payload = self._worker.request(upload, duration=duration, cancel=self.cancel,
                                progress=lambda e: report(start/rate, audio.duration, e.get("message", "等待语音服务")))
                            payload = parse_response(payload, duration)
                            if not payload["text"]:
                                raise RuntimeError("语音服务返回空文本；未将本块标为完成，请检查录音或模型。")
                        finally:
                            upload.unlink(missing_ok=True)
                    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                    atomic_write_json(checkpoint, dict(result=payload, sha256=hashlib.sha256(raw).hexdigest()), private=True)
                if payload["text"]:
                    texts.append(payload["text"])
                    all_timed = all_timed and bool(payload["segments"])
                    segments.extend(Segment(s["start"] + start/rate, s["end"] + start/rate, s["text"])
                                    for s in payload["segments"])
                if payload["language"]:
                    languages.append(payload["language"])
                start = end
                report(end/rate, audio.duration, f"云端转录 · 音频块 {index} 已完成")
        # A partially timed transcript must not masquerade as complete subtitles.
        return "\n".join(texts).strip(), tuple(segments) if all_timed else (), next(iter(languages), self.settings.language)

    def _transcribe(self, input_only_cmd, timeout=7200, prog_key=None,
                    title="", tag="", source_duration=None, identity=None):
        self.last_result = None
        self._last_duration = 0.0
        self._last_transcript = ""
        self._last_segments = []
        self._media_duration = None
        head = f"tr {title} {tag}".rstrip()
        started = time.monotonic()

        def report(completed, total, message):
            events.progress(message, phase="transcribing", unit="seconds", completed=completed,
                            total=total, backend="cloud", model=self.settings.model)
            _prog(prog_key, f"{head} · {message} · {completed/total*100:.1f}%")

        with tempfile.TemporaryDirectory(prefix="icourse-audio-", dir=os.environ.get("ICOURSE_RUN_TEMP")) as tmp:
            events.progress("正在提取音频", phase="decoding")
            _prog(prog_key, f"{head} · 正在提取音频，转录由云端完成")
            audio = decode(input_only_cmd, Path(tmp) / "audio.wav", timeout=timeout,
                           cancel=self.cancel, source_duration=source_duration)
            identity = identity or file_sha256(audio.path)
            cache = self.cache_dir / identity / self.settings.fingerprint
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = FileLock(cache / "transcribe.lock")
            while True:
                self._check_cancel()
                try:
                    lock.acquire(timeout=.2)
                    break
                except LockTimeout:
                    continue
            try:
                text, segments, language = self._chunks(audio, cache, report)
            finally:
                lock.release()
        if not text:
            raise RuntimeError("未识别到语音，请检查录音；不会输出已完成的空转录。")
        warnings = []
        if not audio.source_duration:
            warnings.append("源时长未知，无法核对完整性。")
        if not segments:
            warnings.append("语音服务未提供完整时间戳，已保存文字，不生成 SRT 字幕。")
        result = TranscriptionResult(
            text=text, segments=segments, language=language,
            decoded_duration=audio.duration, source_duration=audio.source_duration,
            backend="cloud", model=self.settings.model, model_revision="",
            complete=audio.source_duration is not None,
            elapsed_seconds=time.monotonic() - started,
            settings_fingerprint=self.settings.fingerprint, warnings=tuple(warnings),
        )
        self.last_result = result
        self._last_transcript = result.text
        self._last_segments = [(s.start, s.end, s.text) for s in segments]
        self._last_duration = result.decoded_duration
        self._media_duration = result.source_duration
        for message in warnings:
            _prog(prog_key, f"{head} · {message}")
        _prog(prog_key, f"{head} · 完成 {len(text)} 字 · 耗时 {result.elapsed_seconds:.1f}s")
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
                         source_duration=info["duration"], identity=cached_file_sha256(video_path))
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
