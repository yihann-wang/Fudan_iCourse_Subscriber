"""Qt desktop shell over the same CLI/pipeline used in batch runs."""

import codecs
import json
import os
import re
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .preferences import SECRET_FIELDS, Preferences, defaults, runtime_environment
from .storage_access import StorageAccessError, check_directory, check_pipeline_storage
from .summary_settings import DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_MINUTES

ROOT = Path(__file__).resolve().parent.parent


class MainWindow(QMainWindow):
    def __init__(self, preferences=None, initial_values=None):
        super().__init__()
        self.preferences = preferences or Preferences()
        self.values = initial_values if initial_values is not None else self.preferences.load(ROOT / ".icourse_gui_config.json")
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.cancelled = False
        self.group_pid = None
        self.closing = False
        self.fields = {}
        self.setWindowTitle("iCourse · 课程与笔记")
        self.resize(940, 790)
        central = QWidget()
        layout = QVBoxLayout(central)
        title = QLabel("iCourse 课程助手")
        title.setStyleSheet("font-size: 23px; font-weight: 600; padding: 8px 0;")
        layout.addWidget(title)
        subtitle = QLabel("下载课程录像，在这台 Mac 上转录，生成字幕与学习笔记。")
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        task = QWidget()
        form = QFormLayout(task)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setVerticalSpacing(12)
        self.mode = QComboBox()
        for label, value in [("下载并生成笔记", "download_and_summarize"), ("只下载课程", "download"),
                             ("为本地课程生成笔记", "summarize"), ("转录一个本地文件", "local_asr")]:
            self.mode.addItem(label, value)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.values.get("mode"))))
        self.fields["mode"] = self.mode
        form.addRow("本次任务", self.mode)
        self.add_text(form, "course_ids", "课程 ID", "多个课程用英文逗号分隔")
        self.add_text(form, "sub_ids", "指定课次（可选）", "留空处理所有可用课次")
        self.add_path(form, "local_media", "本地音视频", is_file=True)
        self.add_path(form, "out_dir", "课程保存位置")
        self.add_path(form, "summary_dir", "笔记保存位置")
        self.add_text(form, "skip_time_periods", "排除时段（可选）", "例如 32890:星期一早上,evening")
        redo_notes = QCheckBox("只重新生成笔记（复用已有转录）")
        redo_notes.setChecked(bool(self.values.get("redo_notes")))
        self.fields["redo_notes"] = redo_notes
        form.addRow("", redo_notes)
        self.overwrite = QCheckBox("重新生成已有转录和笔记")
        self.overwrite.setChecked(bool(self.values.get("overwrite")))
        self.fields["overwrite"] = self.overwrite
        form.addRow("", self.overwrite)
        redo_notes.toggled.connect(lambda checked: self.overwrite.setChecked(False) if checked else None)
        self.overwrite.toggled.connect(lambda checked: redo_notes.setChecked(False) if checked else None)
        self.awake = QCheckBox("运行时保持 Mac 唤醒")
        self.awake.setChecked(bool(self.values.get("keep_awake", True)))
        self.fields["keep_awake"] = self.awake
        form.addRow("", self.awake)
        self.tabs.addTab(task, "任务")

        settings = QWidget()
        config = QFormLayout(settings)
        config.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        config.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        config.setVerticalSpacing(10)
        self.add_text(config, "stu_id", "学号")
        self.add_text(config, "uis_psw", "统一身份认证密码", secret=True)
        self.add_text(config, "llm_base_url_1", "笔记服务地址", "OpenAI 或 Anthropic 兼容服务地址")
        self.add_text(config, "llm_models_1", "笔记模型", "填写服务商提供的模型名称")
        self.add_text(config, "llm_api_key_1", "笔记服务 API Key", secret=True)
        whole_hint = QLabel("整节课一次总结，直接生成一份笔记，保留正常标题和段落。")
        whole_hint.setWordWrap(True)
        config.addRow(whole_hint)
        for name, label, default, low, high, step, suffix in (
            ("llm_output_tokens", "单次输出上限", DEFAULT_OUTPUT_TOKENS, 1024, 131072, 1024, " tokens"),
            ("llm_timeout_minutes", "单次请求最长等待", DEFAULT_TIMEOUT_MINUTES, 1, 60, 1, " 分钟"),
        ):
            field = QSpinBox()
            field.setRange(low, high)
            field.setSingleStep(step)
            field.setSuffix(suffix)
            field.setValue(int(self.values.get(name, default)))
            self.fields[name] = field
            config.addRow(label, field)
        self.backend = QComboBox()
        for label, value in [("自动选择（Apple Silicon 使用 MLX）", "auto"), ("Apple GPU · MLX", "mlx"), ("CPU 备用", "cpu")]:
            self.backend.addItem(label, value)
        self.backend.setCurrentIndex(max(0, self.backend.findData(self.values.get("asr_backend", "auto"))))
        self.fields["asr_backend"] = self.backend
        config.addRow("本地转录", self.backend)
        self.add_text(config, "whisper_model", "转录模型", "large-v3-turbo 或本地模型文件夹")
        self.add_text(config, "whisper_language", "识别语言", "zh；留空自动检测")
        chunk = QSpinBox()
        chunk.setRange(30, 1800)
        chunk.setSingleStep(30)
        chunk.setSuffix(" 秒")
        chunk.setValue(int(self.values.get("chunk_seconds", 300)))
        self.fields["chunk_seconds"] = chunk
        config.addRow("每块音频长度", chunk)
        hint = QLabel("密码和 API Key 保存到 macOS 钥匙串。转录在本机完成；生成笔记会将转录文本发送到你配置的服务。")
        hint.setWordWrap(True)
        config.addRow(hint)
        config_buttons = QHBoxLayout()
        for label, callback in [("保存设置", self.save), ("检查环境", lambda: self.launch("doctor")),
                                ("准备转录模型", lambda: self.launch("prepare-model"))]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            config_buttons.addWidget(button)
        config.addRow(config_buttons)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(settings)
        self.tabs.addTab(settings_scroll, "设置")

        self.status = QLabel("就绪 · Apple Silicon 使用本机 GPU 转录")
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("运行状态会显示在这里。")
        layout.addWidget(self.log, 1)
        actions = QHBoxLayout()
        self.start = QPushButton("开始任务")
        self.start.setDefault(True)
        self.start.clicked.connect(lambda: self.launch("task"))
        self.stop = QPushButton("取消")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        open_folder = QPushButton("打开保存位置")
        open_folder.clicked.connect(self.open_output)
        actions.addWidget(open_folder)
        actions.addStretch()
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        layout.addLayout(actions)
        self.setCentralWidget(central)

    def add_text(self, form, name, label, placeholder="", secret=False):
        field = QLineEdit(str(self.values.get(name, "")))
        field.setPlaceholderText(placeholder)
        if secret:
            field.setEchoMode(QLineEdit.EchoMode.Password)
        self.fields[name] = field
        form.addRow(label, field)

    def add_path(self, form, name, label, is_file=False):
        row = QHBoxLayout()
        field = QLineEdit(str(self.values.get(name, "")))
        button = QPushButton("选择…")

        def choose():
            value = (QFileDialog.getOpenFileName(self, label, str(Path.home()),
                     "音视频 (*.mp4 *.mov *.mkv *.m4a *.mp3 *.wav *.aiff);;所有文件 (*)")[0]
                     if is_file else QFileDialog.getExistingDirectory(self, label, str(Path.home())))
            if value:
                field.setText(value)

        button.clicked.connect(choose)
        row.addWidget(field)
        row.addWidget(button)
        form.addRow(label, row)
        self.fields[name] = field

    def collect(self):
        values = dict(self.values)
        for name, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            elif isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            else:
                values[name] = widget.text() if name in SECRET_FIELDS else widget.text().strip()
        return values

    def save(self):
        try:
            values = self.collect()
            self.preferences.save(values)
            self.values = values
            self.status.setText("设置已保存，密码与 API Key 已存入系统钥匙串")
            return True
        except Exception as exc:
            QMessageBox.warning(self, "设置未保存", str(exc))
            return False

    def command(self, action, values):
        if action != "task":
            return [action]
        if values["overwrite"] and values.get("redo_notes"):
            raise ValueError("请选择只重做笔记，或同时重新转录，二者不能同时启用。")
        if values["mode"] == "local_asr":
            if not Path(values["local_media"]).is_file():
                raise ValueError("请选择要转录的音视频文件。")
            args = ["transcribe", values["local_media"], "--output-dir", values["summary_dir"]]
        else:
            if not values["course_ids"]:
                raise ValueError("请填写课程 ID。")
            if values["mode"] != "summarize" and not (values["stu_id"] and values["uis_psw"]):
                raise ValueError("下载课程需要在设置中填写学号和密码。")
            if values["mode"] != "download" and not all(values[k] for k in ("llm_api_key_1", "llm_base_url_1", "llm_models_1")):
                raise ValueError("生成笔记需要填写服务地址、模型和 API Key。")
            args = ["run", "--mode", values["mode"], "--course-ids", values["course_ids"],
                    "--out-dir", values["out_dir"], "--summary-dir", values["summary_dir"],
                    "--sub-ids", values["sub_ids"], "--skip-time-periods", values["skip_time_periods"]]
        if values["overwrite"]:
            args.append("--overwrite")
        elif values.get("redo_notes"):
            if values["mode"] not in ("summarize", "download_and_summarize"):
                raise ValueError("只重新生成笔记适用于“为本地课程生成笔记”或“下载并生成笔记”。")
            args.append("--redo-notes")
        return args

    def launch(self, action):
        if self.process.state() != QProcess.ProcessState.NotRunning:
            return
        try:
            values = self.collect()
            if action == "task":
                for name in ("out_dir", "summary_dir"):
                    path = Path(values[name] or defaults()[name]).expanduser()
                    values[name] = str((path if path.is_absolute() else ROOT / path).resolve())
            args = self.command(action, values)
            env = runtime_environment(values)
            if action == "task":
                # Request access in the native GUI process. The supervised worker
                # repeats these checks before login or model work.
                if values["mode"] == "local_asr":
                    check_directory(Path(values["summary_dir"]), "转录保存位置", writable=True, create=True)
                else:
                    check_pipeline_storage(Path(values["out_dir"]), Path(values["summary_dir"]),
                                           mode=values["mode"], list_only=False)
        except StorageAccessError as exc:
            QMessageBox.warning(self, "保存位置无法访问", str(exc))
            return
        except ValueError as exc:
            QMessageBox.information(self, "还需要填写", str(exc))
            return
        env["ICOURSE_KEEP_AWAKE"] = "1" if values["keep_awake"] else "0"
        process_env = QProcessEnvironment()
        for key, value in env.items():
            process_env.insert(key, value)
        self.process.setProcessEnvironment(process_env)
        self.process.setWorkingDirectory(str(ROOT))
        self.cancelled = False
        self.buffer = ""
        self.decoder.reset()
        self.log.clear()
        self.progress.setRange(0, 0)
        self.status.setText("正在启动…")
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        self.process.start(sys.executable, ["-m", "src.engine", *args])

    def read_output(self):
        self.buffer += self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.handle_line(line.rstrip())

    def handle_line(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get("event") == "stage":
            stage = {"dl": "下载", "tr": "转录", "sm": "生成笔记"}.get(event["stage"], event["stage"])
            status = {"running": "处理中", "done": "完成", "failed": "失败", "pending": "等待回放"}.get(event["status"], event["status"])
            self.status.setText(f"{stage} · {event['title']} · {status}")
            self.log.appendPlainText(f"{stage} · {event['title']} · {status} {event.get('message', '')}")
            return
        line = re.sub(r"^\[(?:PROG|PFIN):[^]]+\]\s*", "", line)
        if line:
            self.log.appendPlainText(line)
            if len(line) < 180:
                self.status.setText(line)

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.log.appendPlainText("无法启动运行环境，请重新运行安装脚本。")
            self.finished(1)

    def cancel(self):
        self.cancelled = True
        self.status.setText("正在停止任务…")
        pid = int(self.process.processId())
        self.group_pid = pid
        self.signal_group(pid, signal.SIGTERM)
        QTimer.singleShot(2500, lambda: self.kill_remaining(pid))

    def signal_group(self, pid, sig):
        if pid <= 0:
            return
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            if self.process.state() != QProcess.ProcessState.NotRunning and int(self.process.processId()) == pid:
                self.process.terminate() if sig == signal.SIGTERM else self.process.kill()

    def kill_remaining(self, pid):
        self.signal_group(pid, signal.SIGKILL)
        if self.group_pid == pid:
            self.group_pid = None
        if self.process.state() == QProcess.ProcessState.NotRunning:
            self.start.setEnabled(True)
        if self.closing:
            self.close()

    def finished(self, code, *_):
        self.read_output()
        if self.buffer:
            self.handle_line(self.buffer)
            self.buffer = ""
        self.start.setEnabled(not self.group_pid)
        self.stop.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if code == 0 and not self.cancelled else 0)
        self.status.setText("已取消；再次开始会复用已完成的结果" if self.cancelled else
                            "任务完成" if code == 0 else "任务未全部完成，请查看上方原因")

    def open_output(self):
        values = self.collect()
        folder = Path(values["out_dir"] if values["mode"] == "download" else values["summary_dir"])
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        if self.process.state() != QProcess.ProcessState.NotRunning or self.group_pid:
            self.closing = True
            if not self.cancelled:
                self.cancel()
            event.ignore()
        else:
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("iCourse")
    app.setOrganizationName("Fudan iCourse Subscriber")
    try:
        window = MainWindow()
    except Exception as exc:
        QMessageBox.critical(None, "无法读取设置", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
