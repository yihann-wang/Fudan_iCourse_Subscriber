"""Cancellable cloud client. Secrets travel through a private pipe, never argv."""

import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from ..media import CancelledError, windows_subprocess_kwargs


class CloudWorker:
    def __init__(self, settings):
        self.settings = settings.resolved()
        self.process = None
        self.messages = queue.Queue()
        self.lock = threading.Lock()
        atexit.register(self.close)

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.messages = queue.Queue()
        command = [sys.executable, "-m", "src.asr.worker"]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            encoding="utf-8", bufsize=1, cwd=Path(__file__).resolve().parents[2],
            **windows_subprocess_kwargs(),
        )
        process, messages = self.process, self.messages

        def read():
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except json.JSONDecodeError:
                        messages.put(dict(event="error", message="语音服务进程返回了无效数据。"))
            finally:
                messages.put(dict(event="closed"))

        threading.Thread(target=read, daemon=True, name="asr-protocol").start()

    def request(self, path=None, *, cancel=None, progress=None, duration=None, timeout=None):
        with self.lock:
            if cancel is not None and cancel.is_set():
                raise CancelledError("转录已取消。")
            timeout = timeout or (self.settings.timeout_seconds + 15) * (self.settings.retries + 1) + 360
            self._start()
            self.process.stdin.write(json.dumps(dict(op="check" if path is None else "transcribe",
                                                     settings=asdict(self.settings), path=str(path), duration=duration)) + "\n")
            self.process.stdin.flush()
            started = time.monotonic()
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("转录已取消。")
                    if time.monotonic() - started > timeout:
                        raise TimeoutError("转录超过允许的运行时间。")
                    try:
                        message = self.messages.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    event = message.get("event")
                    if event == "result":
                        return message
                    if event in {"error", "closed"}:
                        raise RuntimeError(message.get("message", "语音服务进程意外退出。"))
                    if progress:
                        progress(message)
            except BaseException:
                self.close()
                raise

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.stdin.write('{"op":"close"}\n')
                process.stdin.flush()
                process.wait(timeout=1)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
