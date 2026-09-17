"""Media discovery and cancellable PCM decoding, independent of ASR."""

import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path


class CancelledError(RuntimeError):
    pass


class NoAudioStreamError(RuntimeError):
    pass


class IncompleteAudioError(RuntimeError):
    def __init__(self, message, actual_duration, expected_duration):
        super().__init__(message)
        self.actual_duration = actual_duration
        self.expected_duration = expected_duration


def windows_subprocess_kwargs():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def resolve_media_tool(name):
    binary = name + (".exe" if os.name == "nt" else "")
    exact = os.environ.get(name.upper() + "_PATH")
    if exact:
        if not Path(exact).is_file():
            raise FileNotFoundError(f"{name.upper()}_PATH 指向的文件不存在。")
        return exact
    roots = []
    if os.environ.get("FFMPEG_DIR"):
        roots.append(Path(os.environ["FFMPEG_DIR"]))
    if getattr(sys, "frozen", False):
        bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        roots.extend([bundle / "ffmpeg_bin", bundle])
    found = shutil.which(binary)
    if found:
        return found
    roots.extend([Path("/opt/homebrew/bin"), Path("/usr/local/bin")])
    for root in roots:
        if (root / binary).is_file():
            return str(root / binary)
    raise FileNotFoundError(f"未找到 {name}。Mac 请先安装 ffmpeg，或设置 FFMPEG_DIR。")


def run_media(cmd, *, timeout, cancel=None):
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **windows_subprocess_kwargs())
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise CancelledError("任务已取消。")
            if time.monotonic() - started > timeout:
                raise TimeoutError(f"媒体处理超过 {timeout} 秒。")
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
                return proc.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


def probe(path, *, headers=None, timeout=30, cancel=None):
    cmd = [resolve_media_tool("ffprobe"), "-v", "error"]
    if headers:
        cmd += ["-headers", headers]
    cmd += ["-show_entries", "format=duration:stream=codec_type,duration", "-of", "json", str(path)]
    code, out, _ = run_media(cmd, timeout=timeout, cancel=cancel)
    if code:
        raise RuntimeError("无法读取媒体信息；请检查文件或重新登录获取录像。")
    info = json.loads(out)
    streams = info.get("streams", [])
    durations = [info.get("format", {}).get("duration")]
    durations += [s.get("duration") for s in streams if s.get("codec_type") == "audio"]
    valid = []
    for duration in durations:
        try:
            value = float(duration)
            if value > 0 and math.isfinite(value):
                valid.append(value)
        except (ValueError, TypeError):
            pass
    return {"duration": max(valid) if valid else None,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


@dataclass(frozen=True)
class DecodedAudio:
    path: Path
    duration: float
    source_duration: float | None
    warnings: tuple[str, ...] = ()


def _safe_stereo_channel(path):
    """Detect strong cancellation in distributed samples; otherwise keep both channels."""
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 2:
            return None
        frames, rate = wav.getnframes(), wav.getframerate()
        width = min(frames, rate * 2)
        positions = sorted({round(i * max(0, frames - width) / 15) for i in range(16)})
        energy = [0, 0]
        cancelled = False
        for position in positions:
            wav.setpos(position)
            samples = array("h", wav.readframes(width))
            if sys.byteorder != "little":
                samples.byteswap()
            left, right = samples[::2], samples[1::2]
            left_energy = sum(x*x for x in left)
            right_energy = sum(x*x for x in right)
            energy[0] += left_energy
            energy[1] += right_energy
            # Compare an averaged mono signal with the strongest input channel.
            # A >20 dB loss with audible input indicates opposite-polarity copies,
            # not an ordinary stereo recording with two independent speakers.
            mixed_energy = sum((l+r)**2 for l, r in zip(left, right)) / 4
            loudest = max(left_energy, right_energy)
            if loudest > len(left) * 100**2 and mixed_energy < loudest * .01:
                cancelled = True
        return int(energy[1] > energy[0]) if cancelled else None


def decode(input_cmd, output, *, timeout=7200, cancel=None, source_duration=None, progress=None):
    output = Path(output)
    channels_path = output.with_name(output.stem + ".channels.wav")
    started = time.monotonic()
    # Preserve channels until we can detect phase cancellation. Blind downmixing
    # can erase speech even though either original channel is perfectly audible.
    cmd = list(input_cmd) + ["-nostdin", "-y", "-vn", "-map", "0:a:0",
                             "-ar", "16000", "-c:a", "pcm_s16le", str(channels_path)]
    code, _, err = run_media(cmd, timeout=timeout, cancel=cancel)
    stderr = err.decode(errors="replace")
    if code:
        if "matches no streams" in stderr or "does not contain any stream" in stderr:
            raise NoAudioStreamError("录像不含音频轨道。")
        # ffmpeg errors can contain signed URLs and cookies; do not expose them.
        raise RuntimeError(f"音频解码失败（ffmpeg 退出码 {code}）。请检查文件完整性或网络连接。")
    with wave.open(str(channels_path), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
        channels = wav.getnchannels()
    if duration <= 0:
        raise RuntimeError("解码后的音频为空。")
    if source_duration is None:
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
        if match:
            h, m, s = map(float, match.groups())
            source_duration = h * 3600 + m * 60 + s
    if source_duration and duration < source_duration - max(2.0, source_duration * 0.01):
        raise IncompleteAudioError(
            f"音频仅收到 {duration:.1f}/{source_duration:.1f} 秒，请重试。",
            duration, source_duration,
        )
    warnings = ()
    try:
        if channels == 1:
            channels_path.replace(output)
        else:
            channel = _safe_stereo_channel(channels_path)
            filters = [] if channel is None else ["-af", f"pan=mono|c0=c{channel}"]
            if channel is not None:
                warnings = (f"检测到左右声道抵消，已使用第 {channel + 1} 个声道保留讲话声音。",)
                if progress:
                    progress(warnings[0])
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("音频提取超时。")
            code, _, _ = run_media([resolve_media_tool("ffmpeg"), "-nostdin", "-v", "error", "-y",
                "-i", str(channels_path), *filters, "-ac", "1", "-c:a", "pcm_s16le", str(output)],
                timeout=remaining, cancel=cancel)
            if code:
                raise RuntimeError("音频声道处理失败。")
        with wave.open(str(output), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getnframes() / wav.getframerate() < duration - .01:
                raise RuntimeError("声道处理后的音频不完整。")
    finally:
        channels_path.unlink(missing_ok=True)
    return DecodedAudio(output, duration, source_duration, warnings)
