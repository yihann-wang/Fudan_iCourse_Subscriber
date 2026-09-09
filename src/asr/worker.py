"""Persistent ASR subprocess. stdin/stdout is a versioned JSON-lines protocol."""

import contextlib
import json
import math
import sys
import time
import wave

from .types import ASRSettings


def merge_chunk_segments(raw, *, read_start, core_start, core_end, duration):
    """An overlapping decode owns a segment when its midpoint is in its core."""
    result = []
    for segment in raw:
        start = max(0.0, float(segment["start"]) + read_start)
        end = min(duration, float(segment["end"]) + read_start)
        text = str(segment.get("text", "")).strip()
        if not (math.isfinite(start) and math.isfinite(end)) or not text or end <= start:
            continue
        midpoint = (start + end) / 2
        if core_start <= midpoint < core_end or (core_end == duration and midpoint == duration):
            result.append(dict(start=start, end=end, text=text))
    return result


def transcribe_chunks(backend, settings, path, emit):
    import numpy as np

    segments = []
    language = settings.language
    started = time.monotonic()
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        if rate != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("ASR 输入必须是单声道 16 kHz PCM16 WAV。")
        frames = wav.getnframes()
        duration = frames / rate
        step = settings.chunk_seconds * rate
        overlap = round(settings.overlap_seconds * rate)
        for core in range(0, frames, step):
            left = max(0, core - overlap)
            right = min(frames, core + step + overlap)
            wav.setpos(left)
            audio = np.frombuffer(wav.readframes(right - left), dtype="<i2").astype(np.float32) / 32768.0
            # Skip digital silence only. This is deliberately not a speech
            # detector and must not discard quiet classroom speech.
            if float(np.max(np.abs(audio))) < 1e-5:
                raw = []
            else:
                raw, language = backend.transcribe(audio)
            segments.extend(merge_chunk_segments(
                raw, read_start=left / rate, core_start=core / rate,
                core_end=min(frames, core + step) / rate, duration=duration,
            ))
            emit(dict(event="progress", completed=min(frames, core + step) / rate,
                      total=duration, segments=len(segments), elapsed=time.monotonic() - started))
    peak = None
    if settings.backend == "mlx":
        import mlx.core as mx
        peak = mx.get_peak_memory()
    return dict(segments=segments, language=language, revision=backend.revision,
                inference_seconds=time.monotonic() - started, peak_memory_bytes=peak)


def main():
    wire = sys.stdout

    def emit(message):
        wire.write(json.dumps({"protocol": 1, **message}, ensure_ascii=False) + "\n")
        wire.flush()

    backend = None
    current = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("op") == "close":
                break
            settings = ASRSettings(**request["settings"]).resolved()
            with contextlib.redirect_stdout(sys.stderr):
                if backend is None or current != settings:
                    emit(dict(event="loading", backend=settings.backend, model=settings.model))
                    from .backends import create_backend
                    backend = create_backend(settings)
                    current = settings
                    emit(dict(event="ready", revision=backend.revision))
                result = ({} if request.get("op") == "prepare" else
                          transcribe_chunks(backend, settings, request["path"], emit))
            emit({"event": "result", **result, "revision": backend.revision})
        except Exception as exc:
            emit(dict(event="error", error_type=type(exc).__name__, message=str(exc)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
