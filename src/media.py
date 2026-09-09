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


def decode(input_cmd, output, *, timeout=7200, cancel=None, source_duration=None):
    cmd = list(input_cmd) + ["-nostdin", "-y", "-vn", "-map", "0:a:0", "-ac", "1",
                             "-ar", "16000", "-c:a", "pcm_s16le", str(output)]
    code, _, err = run_media(cmd, timeout=timeout, cancel=cancel)
    stderr = err.decode(errors="replace")
    if code:
        if "matches no streams" in stderr or "does not contain any stream" in stderr:
            raise NoAudioStreamError("录像不含音频轨道。")
        # ffmpeg errors can contain signed URLs and cookies; do not expose them.
        raise RuntimeError(f"音频解码失败（ffmpeg 退出码 {code}）。请检查文件完整性或网络连接。")
    with wave.open(str(output), "rb") as wav:
        duration = wav.getnframes() / wav.getframerate()
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
    return DecodedAudio(Path(output), duration, source_duration)
