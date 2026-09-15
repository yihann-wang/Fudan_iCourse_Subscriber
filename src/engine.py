"""One supervised process group for GUI and command-line runs."""

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid

from . import task_events as events


def main():
    if os.name == "posix":
        try:
            os.setsid()
        except PermissionError:
            pass
        # SIGTERM from the desktop shell still executes cleanup in this
        # supervisor. Descendant processes receive the same group signal.
        def terminate(_signum, _frame):
            raise SystemExit(143)
        signal.signal(signal.SIGTERM, terminate)
    awake = None
    if sys.platform == "darwin" and os.environ.get("ICOURSE_KEEP_AWAKE") == "1":
        awake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
    temporary = tempfile.TemporaryDirectory(prefix="icourse-run-")
    os.environ["ICOURSE_RUN_TEMP"] = temporary.name
    run_id = os.environ.setdefault("ICOURSE_RUN_ID", uuid.uuid4().hex)
    events.begin_run(run_id)
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            events.emit("heartbeat", pid=os.getpid())
            stopped.wait(5)

    monitor = threading.Thread(target=heartbeat, daemon=True)
    monitor.start()
    try:
        from .cli import main as cli
        with contextlib.redirect_stdout(sys.stderr) if events.enabled() else contextlib.nullcontext():
            code = cli()
        stopped.set()
        monitor.join(timeout=1)
        events.emit("run_finished", status="failed" if code else "done")
        # Threads can own subprocesses when a user interrupts a running batch.
        if code == 130 and os.name == "posix":
            os.killpg(os.getpgrp(), signal.SIGTERM)
        return code
    except BaseException as exc:
        stopped.set()
        monitor.join(timeout=1)
        events.emit("run_finished", status="failed", message=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        stopped.set()
        monitor.join(timeout=1)
        if awake and awake.poll() is None:
            awake.terminate()
            awake.wait(timeout=3)
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
