"""Offline native-launcher smoke test; never loads saved settings or credentials."""

import json
import sys


def main():
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication

    from .mac_gui import MainWindow
    from .preferences import defaults

    app = QApplication([])
    window = MainWindow(initial_values=defaults())
    app.processEvents()
    process = QProcess()
    process.start(sys.executable, ["-I", "-c",
        "import json,sys; import src.engine,src.pipeline; "
        "print(json.dumps(dict(executable=sys.executable,prefix=sys.prefix)))"])
    if not process.waitForFinished(30000):
        process.kill()
        process.waitForFinished(5000)
        raise RuntimeError("后台运行环境启动超时。")
    if process.exitCode() != 0 or process.exitStatus() != QProcess.ExitStatus.NormalExit:
        raise RuntimeError("后台运行环境无法载入。")
    child = json.loads(bytes(process.readAllStandardOutput()))
    report = dict(bundle_id=sys.argv[1], executable=sys.executable, prefix=sys.prefix,
                  isolated=sys.flags.isolated, modes=window.mode.count(), child=child)
    window.close()
    if (report["bundle_id"] != "local.fudan.icourse" or not report["isolated"]
            or report["modes"] != 4 or child["executable"] != sys.executable
            or child["prefix"] != sys.prefix):
        raise RuntimeError("App 身份或独立运行环境检查失败。")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
