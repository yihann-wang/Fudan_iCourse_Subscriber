"""Qt desktop shell over the same CLI/pipeline used in batch runs."""

import codecs
import json
import os
import re
import signal
import sys
import uuid
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .preferences import SECRET_FIELDS, Preferences, defaults, runtime_environment
from .asr.types import DASHSCOPE_DEFAULT_MODEL, DASHSCOPE_MODEL_SUGGESTIONS
from .storage_access import StorageAccessError, check_directory, check_pipeline_storage
from .summary_settings import DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_MINUTES
from .task_events import redact, set_secrets
from .task_panel import TaskPanel

ROOT = Path(__file__).resolve().parent.parent


class ScrollSafeSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()  # Scroll the settings page; edit numbers by typing/arrows.


class ScrollSafeComboBox(QComboBox):
    def wheelEvent(self, event):
        event.ignore()


class MainWindow(QMainWindow):
    def __init__(self, preferences=None, initial_values=None):
        super().__init__()
        self.preferences = preferences or Preferences()
        self.values = initial_values if initial_values is not None else self.preferences.load(ROOT / ".icourse_gui_config.json")
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.started.connect(lambda: setattr(self, "engine_pid", int(self.process.processId())))
        self.engine_pid = None
        self.buffer = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.error_buffer = ""
        self.error_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.exit_result = None
        self.run_values = None
        self.cancelled = False
        self.group_pid = None
        self.closing = False
        self.fields = {}
        self.setWindowTitle("iCourse · 课程与笔记")
        self.resize(1060, 850)
        central = QWidget()
        layout = QVBoxLayout(central)
        title = QLabel("iCourse 课程助手")
        title.setStyleSheet("font-size: 23px; font-weight: 600; padding: 8px 0;")
        layout.addWidget(title)
        subtitle = QLabel("下载课程录像，使用云端语音转录，生成整课学习笔记。")
        layout.addWidget(subtitle)
        self.tabs = QTabWidget()
        self.toggle_settings = QPushButton("收起任务设置")
        self.toggle_settings.setCheckable(True)
        self.toggle_settings.setChecked(True)
        self.toggle_settings.toggled.connect(self.show_settings)
        layout.addWidget(self.toggle_settings)
        self.panes = QSplitter(Qt.Orientation.Vertical)
        self.panes.setChildrenCollapsible(False)
        self.panes.setHandleWidth(8)
        self.panes.setStyleSheet("QSplitter::handle:vertical { background: palette(mid); margin: 2px 0; }")
        self.panes.addWidget(self.tabs)
        self.tabs.setMinimumHeight(240)
        layout.addWidget(self.panes, 1)
        task = QWidget()
        form = QFormLayout(task)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setVerticalSpacing(12)
        self.mode = ScrollSafeComboBox()
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
        task_scroll = QScrollArea()
        task_scroll.setWidgetResizable(True)
        task_scroll.setWidget(task)
        self.tabs.addTab(task_scroll, "任务")

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
            field = ScrollSafeSpinBox()
            field.setRange(low, high)
            field.setSingleStep(step)
            field.setSuffix(suffix)
            field.setValue(int(self.values.get(name, default)))
            self.fields[name] = field
            config.addRow(label, field)
        reset_budget = QPushButton("恢复笔记推荐参数（65536 tokens / 20 分钟）")
        reset_budget.clicked.connect(self.reset_note_budget)
        config.addRow("", reset_budget)
        provider = ScrollSafeComboBox()
        provider.addItem("OpenAI 兼容音频接口（硅基流动 / Groq 等）", "openai")
        provider.addItem("百炼录音文件接口（异步识别）", "dashscope")
        provider.setCurrentIndex(max(0, provider.findData(self.values.get("asr_provider", "openai"))))
        self.fields["asr_provider"] = provider
        config.addRow("语音服务", provider)
        self.add_text(config, "asr_base_url", "语音服务地址", "OpenAI 兼容的音频上传接口，例如 https://api.siliconflow.cn/v1")
        self.add_text(config, "asr_model", "语音模型", "填写服务商提供的完整模型名称，可随时更换")
        self.add_text(config, "asr_api_key", "语音服务 API Key", secret=True)
        self.add_text(config, "asr_language", "语音语言（可选）", "默认留空；服务支持时可填 zh 或 en")
        self.add_text(config, "asr_prompt", "专业术语（可选）", "仅在语音服务支持 prompt 时填写")
        response_format = ScrollSafeComboBox()
        for label, value in [("服务默认（推荐）", ""), ("JSON 文本", "json"),
                             ("带时间戳 JSON", "verbose_json"), ("纯文本", "text"), ("SRT 字幕", "srt")]:
            response_format.addItem(label, value)
        response_format.setCurrentIndex(max(0, response_format.findData(self.values.get("asr_response_format", ""))))
        self.fields["asr_response_format"] = response_format
        config.addRow("语音返回格式", response_format)
        self.add_text(config, "asr_dashscope_base_url", "百炼服务地址", "北京：https://dashscope.aliyuncs.com/api/v1")
        ali_model = ScrollSafeComboBox()
        ali_model.setEditable(True)
        ali_model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        ali_model.addItems(DASHSCOPE_MODEL_SUGGESTIONS)
        ali_model.setCurrentText(self.values.get("asr_dashscope_model", DASHSCOPE_DEFAULT_MODEL))
        ali_model.lineEdit().setPlaceholderText("输入服务商提供的完整模型 ID；下拉项只是示例")
        self.fields["asr_dashscope_model"] = ali_model
        config.addRow("百炼语音模型", ali_model)
        self.add_text(config, "asr_dashscope_api_key", "百炼 API Key", "填写所选地域的百炼密钥，与硅基流动密钥分开保存", secret=True)
        alignment = ScrollSafeComboBox()
        for label, value in [("服务默认（推荐）", "default"), ("开启（模型支持时）", "enabled"), ("关闭", "disabled")]:
            alignment.addItem(label, value)
        alignment.setCurrentIndex(max(0, alignment.findData(self.values.get("asr_dashscope_timestamp_alignment", "default"))))
        self.fields["asr_dashscope_timestamp_alignment"] = alignment
        config.addRow("时间戳校准", alignment)
        ali_hint = QLabel("自动上传音频并生成原文和同步字幕。支持中断后继续查询。"
                          "模型需支持录音文件异步接口和带时间戳的结果；语言可留空自动识别。"
                          "已有课程补字幕需勾选“重新生成已有转录和笔记”。")
        ali_hint.setWordWrap(True)
        config.addRow(ali_hint)

        def show_asr_provider():
            aliyun = provider.currentData() == "dashscope"
            for name in ("asr_base_url", "asr_model", "asr_api_key", "asr_prompt", "asr_response_format"):
                config.setRowVisible(self.fields[name], not aliyun)
            for name in ("asr_dashscope_base_url", "asr_dashscope_model", "asr_dashscope_api_key", "asr_dashscope_timestamp_alignment"):
                config.setRowVisible(self.fields[name], aliyun)
            config.setRowVisible(ali_hint, aliyun)
            self.fields["asr_language"].setPlaceholderText("留空自动识别；语言数量限制以所选模型为准" if aliyun else "默认留空；服务支持时可填 zh 或 en")

        provider.currentIndexChanged.connect(show_asr_provider)
        show_asr_provider()
        for name, label, default, low, high, suffix in (
            ("chunk_seconds", "音频块最长时长", 300, 30, 1800, " 秒"),
            ("asr_max_upload_mb", "单次上传大小上限", 20, 1, 100, " MB"),
            ("asr_timeout_seconds", "语音请求等待上限", 300, 10, 3600, " 秒"),
            ("asr_retries", "语音请求重试次数", 2, 0, 5, " 次"),
        ):
            field = ScrollSafeSpinBox()
            field.setRange(low, high)
            field.setSuffix(suffix)
            field.setValue(int(self.values.get(name, default)))
            self.fields[name] = field
            config.addRow(label, field)
        hint = QLabel("密码和 API Key 保存到 macOS 钥匙串。音频发送至语音服务，合并后的整课文字发送至笔记服务。"
                      "只有语音服务返回真实时间戳时才生成字幕。已有转录可以继续复用。")
        hint.setWordWrap(True)
        config.addRow(hint)
        config_buttons = QHBoxLayout()
        for label, callback in [("保存设置", self.save), ("检查环境", lambda: self.launch("doctor")),
                                ("检查语音连接", lambda: self.launch("check-asr"))]:
            button = QPushButton(label)
            button.clicked.connect(callback)
            config_buttons.addWidget(button)
        config.addRow(config_buttons)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setWidget(settings)
        self.tabs.addTab(settings_scroll, "设置")

        log_dir = Path.home() / "Library/Logs/Fudan iCourse Subscriber" if preferences is None and initial_values is None else None
        self.panel = TaskPanel(self, log_dir=log_dir)
        self.status, self.progress, self.log = self.panel.status, self.panel.progress, self.panel.log
        self.panel.set_compact(True)
        self.progress_scroll = QScrollArea()
        self.progress_scroll.setWidgetResizable(True)
        self.progress_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.progress_scroll.setWidget(self.panel)
        self.progress_scroll.setMinimumHeight(90)
        self.panes.addWidget(self.progress_scroll)
        self.panes.setStretchFactor(0, 1)
        self.panes.setStretchFactor(1, 0)
        self.panes.setSizes([620, 90])
        self.panes.handle(1).setToolTip("上下拖动，调整任务设置和进度区域的高度")
        actions = QHBoxLayout()
        self.start = QPushButton("开始任务")
        self.start.setDefault(True)
        self.start.clicked.connect(lambda: self.launch("task"))
        self.stop = QPushButton("停止任务")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self.cancel)
        open_folder = QPushButton("打开保存位置")
        open_folder.clicked.connect(self.open_output)
        actions.addWidget(open_folder)
        self.retry = QPushButton("仅重试失败课次")
        self.retry.setEnabled(False)
        self.retry.clicked.connect(self.retry_failed)
        actions.addWidget(self.retry)
        actions.addStretch()
        actions.addWidget(self.stop)
        actions.addWidget(self.start)
        layout.addLayout(actions)
        self.setCentralWidget(central)

    def show_settings(self, checked):
        self.tabs.setVisible(checked)
        self.toggle_settings.setText("收起任务设置" if checked else "展开任务设置")
        if checked and hasattr(self, "panel"):
            height = max(400, self.panes.height())
            progress_height = 90 if self.panel.model is None else int(height * 0.4)
            self.panes.setSizes([height - progress_height, progress_height])

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
                values[name] = widget.currentText().strip() if widget.isEditable() else widget.currentData()
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

    def reset_note_budget(self):
        self.fields["llm_output_tokens"].setValue(DEFAULT_OUTPUT_TOKENS)
        self.fields["llm_timeout_minutes"].setValue(DEFAULT_TIMEOUT_MINUTES)

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

    def launch(self, action, retry_tasks=None):
        if self.process.state() != QProcess.ProcessState.NotRunning or self.group_pid:
            return
        try:
            values = self.collect()
            if retry_tasks and self.run_values:
                # Preserve the run's input/output selection, but allow corrected credentials/models.
                for key in ("mode", "out_dir", "summary_dir", "local_media"):
                    values[key] = self.run_values[key]
                values.update(course_ids=",".join(dict.fromkeys(t.course_id for t in retry_tasks)),
                              sub_ids="", skip_time_periods="", overwrite=False, redo_notes=False)
            if action == "task":
                for name in ("out_dir", "summary_dir"):
                    path = Path(values[name] or defaults()[name]).expanduser()
                    values[name] = str((path if path.is_absolute() else ROOT / path).resolve())
            args = self.command(action, values)
            if retry_tasks and values["mode"] != "local_asr":
                for task in retry_tasks:
                    stage = next(name for name, state in task.stages.items() if state.status == "failed")
                    args.extend(["--target", f"{task.course_id}:{task.sub_id}",
                                 "--resume-stage", f"{task.course_id}:{task.sub_id}:{stage}"])
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
        run_id = uuid.uuid4().hex
        env.update(ICOURSE_EVENTS="json", ICOURSE_RUN_ID=run_id)
        set_secrets([values.get(k) for k in SECRET_FIELDS] + [values.get("stu_id")])
        process_env = QProcessEnvironment()
        for key, value in env.items():
            process_env.insert(key, value)
        self.process.setProcessEnvironment(process_env)
        self.process.setWorkingDirectory(str(ROOT))
        self.cancelled = False
        self.exit_result = None
        self.engine_pid = None
        self.run_values = dict(values)
        self.buffer = ""
        self.error_buffer = ""
        self.decoder.reset()
        self.error_decoder.reset()
        self.panel.begin(run_id)
        self.toggle_settings.setChecked(False)
        self.tabs.setEnabled(False)
        self.start.setEnabled(False)
        self.stop.setEnabled(True)
        self.retry.setEnabled(False)
        self.process.start(sys.executable, ["-m", "src.engine", *args])

    def retry_failed(self):
        if self.panel.model:
            tasks = [t for t in self.panel.model.tasks.values() if t.outcome == "failed"]
            if tasks:
                self.launch("task", retry_tasks=tasks)

    def read_output(self):
        self.buffer += self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.handle_line(line.rstrip())
        if len(self.buffer) > 65536:
            self.panel.diagnostic("忽略超长的非标准输出。")
            self.buffer = ""

    def read_error(self):
        self.error_buffer += self.error_decoder.decode(bytes(self.process.readAllStandardError()))
        self.error_buffer = self.error_buffer.replace("\r", "\n")
        while "\n" in self.error_buffer:
            line, self.error_buffer = self.error_buffer.split("\n", 1)
            self.panel.diagnostic(line)
        if len(self.error_buffer) > 16000:
            self.panel.diagnostic(self.error_buffer[:16000])
            self.error_buffer = ""

    def handle_line(self, line):
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict) and event.get("event") == "icourse":
            # Redact every string even if an older engine did not sanitize it.
            event = self.sanitize_event(event)
            self.panel.apply_event(event)
            return
        line = re.sub(r"^\[(?:PROG|PFIN):[^]]+\]\s*", "", line)
        if line:
            self.panel.diagnostic(line)

    @staticmethod
    def sanitize_event(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {k: MainWindow.sanitize_event(v) for k, v in value.items()}
        if isinstance(value, list):
            return [MainWindow.sanitize_event(v) for v in value]
        return value

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.panel.diagnostic("无法启动运行环境，请重新运行安装脚本。")
            if self.panel.model:
                self.panel.model.final = "failed"
                self.panel.model.record({}, "无法启动运行环境，请重新运行安装脚本。")
            self.finished(1)

    def cancel(self):
        if self.cancelled or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.cancelled = True
        if self.panel.model:
            self.panel.model.stopping = True
            self.panel.render()
        self.stop.setEnabled(False)
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
            self.complete_exit()
        if self.closing:
            self.close()

    def finished(self, code, exit_status=QProcess.ExitStatus.NormalExit):
        self.read_output()
        self.read_error()
        if self.buffer:
            self.handle_line(self.buffer)
            self.buffer = ""
        if self.error_buffer:
            self.panel.diagnostic(self.error_buffer)
            self.error_buffer = ""
        self.exit_result = (code, exit_status == QProcess.ExitStatus.CrashExit)
        self.stop.setEnabled(False)
        if exit_status == QProcess.ExitStatus.CrashExit and self.engine_pid and not self.group_pid:
            try:
                os.killpg(self.engine_pid, 0)
            except ProcessLookupError:
                pass
            else:
                self.group_pid = self.engine_pid
                self.signal_group(self.engine_pid, signal.SIGTERM)
                QTimer.singleShot(2500, lambda pid=self.engine_pid: self.kill_remaining(pid))
        if not self.group_pid:
            self.complete_exit()

    def complete_exit(self):
        if self.exit_result is None:
            return
        code, crashed = self.exit_result
        if self.panel.model and not self.panel.model.exited:
            self.panel.model.finish(code, cancelled=self.cancelled, crashed=crashed)
            self.panel.model.record({}, self.panel.model.heading)
            self.panel.render()
            self.retry.setEnabled(bool(self.panel.model.counts()["failed"]))
            self.panel.save_result()
        self.start.setEnabled(True)
        self.tabs.setEnabled(True)
        self.panel.close_log()
        if self.closing:
            self.close()

    def open_output(self):
        values = self.collect()
        folder = Path(values["out_dir"] if values["mode"] == "download" else values["summary_dir"])
        try:
            check_directory(folder, "保存位置", writable=True, create=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except (StorageAccessError, OSError) as exc:
            QMessageBox.warning(self, "无法打开保存位置", str(exc))

    def closeEvent(self, event):
        if self.process.state() != QProcess.ProcessState.NotRunning or self.group_pid:
            self.closing = True
            if not self.cancelled:
                self.cancel()
            event.ignore()
        else:
            self.panel.close_log()
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
