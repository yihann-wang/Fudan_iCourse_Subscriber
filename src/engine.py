"""One supervised process group for GUI and command-line runs."""

import os
import signal
import subprocess
import sys
import tempfile


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
    try:
        from .cli import main as cli
        code = cli()
        # Threads can own subprocesses when a user interrupts a running batch.
        if code == 130 and os.name == "posix":
            os.killpg(os.getpgrp(), signal.SIGTERM)
        return code
    finally:
        if awake and awake.poll() is None:
            awake.terminate()
            awake.wait(timeout=3)
        temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
