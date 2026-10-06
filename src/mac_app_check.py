"""Offline native-launcher smoke test; never loads saved settings or credentials."""

import json
import importlib.metadata
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    with TemporaryDirectory(prefix="icourse-self-test-") as temporary:
        return check(Path(temporary) / "elearning.json")


def check(config_store):
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication

    from .mac_gui import MainWindow
    from .preferences import defaults

    app = QApplication([])
    window = MainWindow(initial_values=defaults(), elearning_config_store=config_store)
    window.show()
    window.tabs.setCurrentIndex(window.elearning_index)
    app.processEvents()
    panel = window.elearning
    if window.fields["stu_id"].text() or window.fields["uis_psw"].text():
        raise RuntimeError("离线自测必须使用空白默认设置。")
    panel.refresh_button.click()
    missing_credentials_inline = not panel.is_running() and "请先在「设置」填写学号和密码" in panel.status.text()
    credential_calls = []
    def never_read_settings_credentials():
        credential_calls.append(True)
        raise RuntimeError("离线自测不得调用统一凭据提供器。")
    panel.credential_provider = never_read_settings_credentials
    panel.launch("demo")
    deadline = time.monotonic() + 20
    while panel.is_running() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    if panel.is_running():
        panel.process.kill()
        panel.process.waitForFinished(3000)
        raise RuntimeError("eLearning 离线操作测试超时。")
    demo_text = panel.output.toPlainText()
    demo_ok = panel.process.exitCode() == 0 and all(value in demo_text for value in
        ["未访问学校", "示例压缩包.zip", "示例代码.py", "示例网页.html"])
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
                  version=importlib.metadata.version("fudan-icourse-subscriber"),
                  isolated=sys.flags.isolated, modes=window.mode.count(), child=child,
                  asr_providers=[window.fields["asr_provider"].itemData(i)
                                 for i in range(window.fields["asr_provider"].count())],
                  editable_asr_model=window.fields["asr_dashscope_model"].isEditable())
    repair = window.command("redownload-videos", dict(defaults(),
        course_ids="12345", sub_ids="123456", stu_id="offline", uis_psw="offline"))
    report["video_repair"] = dict(visible=bool(window.redownload.text()),
        download_only=repair[repair.index("--mode") + 1] == "download",
        explicit="--redownload" in repair)
    from .elearning_helper.config import load_config
    from .elearning_helper.sync import eligible
    configuration = load_config(Path(__file__).parent / "elearning_helper/config.json")
    course = configuration.courses[0]
    report["elearning"] = dict(tab=window.tabs.tabText(window.elearning_index), demo_ok=demo_ok,
        settings_editor_available=window.settings.isAncestorOf(window.settings.elearning_editor),
        course_count=len(configuration.courses), exclusive_limit=configuration.max_bytes,
        name_preservation_visible="同名保留" in demo_text,
        all_types_allowed=all(eligible({"display_name": name, "size": 1}, course, configuration) is None
                              for name in ["教材.pdf", "作业.docx", "代码.py", "archive.zip", "page.html"]),
        exact_limit_rejected=eligible({"display_name": "file.bin", "size": 50_000_000}, course, configuration) is not None,
        no_extra_roots=all(not c.dedup_directories for c in configuration.courses),
        structured_task_rows=panel.task_tree.topLevelItemCount(),
        details_collapsed=not panel.details_toggle.isChecked(),
        settings_credentials_unused=not credential_calls,
        missing_credentials_inline=missing_credentials_inline)
    report["workspaces"] = dict(
        pages=[window.tabs.tabText(i) for i in range(window.tabs.count())],
        all_fields_in_settings=all(window.settings.isAncestorOf(field) for field in window.fields.values()),
        logs_collapsed=window.panel.log.isHidden(),
        config_inputs_hidden=panel.config_path.isHidden() and panel.edit_config.isHidden())
    window.close()
    if (report["bundle_id"] != "local.fudan.icourse" or not report["isolated"]
            or report["modes"] != 2 or child["executable"] != sys.executable
            or report["workspaces"]["pages"] != ["录像与笔记", "eLearning 与作业", "设置"]
            or not all(report["workspaces"][key] for key in ["all_fields_in_settings", "logs_collapsed", "config_inputs_hidden"])
            or report["asr_providers"] != ["openai", "dashscope"]
            or not report["editable_asr_model"]
            or not all(report["video_repair"].values())
            or not all(report["elearning"][key] for key in ["demo_ok", "all_types_allowed", "exact_limit_rejected", "no_extra_roots", "name_preservation_visible", "settings_editor_available"])
            or report["elearning"]["structured_task_rows"] < 1
            or not report["elearning"]["details_collapsed"]
            or not report["elearning"]["settings_credentials_unused"]
            or not report["elearning"]["missing_credentials_inline"]
            or child["prefix"] != sys.prefix):
        raise RuntimeError("App 身份或独立运行环境检查失败。")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
