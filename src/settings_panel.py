"""One editable settings surface; workers receive committed snapshots only."""

from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget, QScrollArea,
    QComboBox, QSpinBox, QCheckBox, QLineEdit, QPushButton, QLabel, QFileDialog,
)
from .preferences import SECRET_FIELDS
from .asr.types import DASHSCOPE_DEFAULT_MODEL, DASHSCOPE_MODEL_SUGGESTIONS
from .summary_settings import DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_MINUTES
from .elearning_settings import ElearningSettingsEditor, editable_config
from .ui_style import ArrowComboBox, CheckBox


class ScrollSafeSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class ScrollSafeComboBox(ArrowComboBox):
    def wheelEvent(self, event):
        event.ignore()


class SettingsPanel(QWidget):
    changed = Signal()
    saveRequested = Signal()
    checkRequested = Signal(str)

    def __init__(self, values, config_source, parent=None):
        super().__init__(parent)
        self.values = deepcopy(values)
        self.fields = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(16)
        note = QLabel("保存后用于下一次启动；正在运行的任务继续使用原配置。")
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.sections = QTabWidget()
        self.sections.setObjectName("settingsSections")
        self.sections.setDocumentMode(True)
        self.sections.tabBar().setDrawBase(False)
        layout.addWidget(self.sections, 1)
        account, account_form = self.add_section("学校账号")
        task, form = self.add_section("录像与笔记")
        elearning, elearning_layout = self.add_section("eLearning", form=False)
        cloud, config = self.add_section("语音与笔记服务")
        advanced, advanced_form = self.add_section("高级与检查")
        self.add_text(account_form, "stu_id", "学号")
        self.add_text(account_form, "uis_psw", "统一身份认证密码", secret=True)
        account_note = QLabel("两组功能共用学校账号。密码和 API Key 保存在本机 settings.json 中。")
        account_note.setWordWrap(True)
        account_form.addRow(account_note)
        self.mode = ScrollSafeComboBox()
        for label, value in [("下载录像并生成笔记", "download_and_summarize"), ("只下载录像", "download")]:
            self.mode.addItem(label, value)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.values.get("mode"))))
        self.fields["mode"] = self.mode
        form.addRow("任务方式", self.mode)
        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        form.addRow(self.mode_hint)

        def show_mode():
            self.mode_hint.setText(
                "下载缺少的录像，复用已有笔记；缺少笔记时，复用已有转录或重新转录后生成。云端处理可能产生费用。"
                if self.mode.currentData() == "download_and_summarize" else
                "只下载缺少的录像，保留已有文件，不调用语音和笔记服务。")

        self.mode.currentIndexChanged.connect(show_mode)
        show_mode()
        self.add_text(form, "course_ids", "课程 ID", "多个课程用英文逗号分隔")
        self.add_text(form, "sub_ids", "指定课次（可选）", "留空处理所有可用课次")
        self.add_path(form, "out_dir", "课程保存位置")
        self.add_path(form, "summary_dir", "笔记保存位置")
        self.add_text(form, "skip_time_periods", "排除时段（可选）", "例如 32890:星期一早上,evening")
        self.awake = CheckBox("运行时保持 Mac 唤醒")
        self.awake.setChecked(bool(self.values.get("keep_awake", True)))
        self.fields["keep_awake"] = self.awake
        form.addRow("", self.awake)

        self.elearning_editor = ElearningSettingsEditor(
            config_source, config_source, data=values["elearning"])
        elearning_layout.addWidget(self.elearning_editor)
        self.import_button = QPushButton("导入 eLearning 配置…")
        self.import_button.clicked.connect(lambda: self.import_config())
        elearning_layout.addWidget(self.import_button)
        elearning_layout.addStretch()
        advanced_form.addRow(self.elearning_editor.advanced)
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
            advanced_form.addRow(label, field)
        reset_budget = QPushButton("恢复笔记推荐参数（65536 tokens / 20 分钟）")
        reset_budget.clicked.connect(self.reset_note_budget)
        advanced_form.addRow("", reset_budget)
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
                          "已有转录和笔记会保留，切换模型不会自动重做旧课。")
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
            advanced_form.addRow(label, field)

        checks = QHBoxLayout()
        self.check_buttons = []
        for label, action in [("检查环境", "doctor"), ("检查语音连接", "check-asr")]:
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=action: self.checkRequested.emit(value))
            self.check_buttons.append(button)
            checks.addWidget(button)
        advanced_form.addRow(checks)
        self.check_note = QLabel("录像任务结束后可进行环境或语音连接检查。")
        self.check_note.setWordWrap(True)
        self.check_note.hide()
        advanced_form.addRow(self.check_note)
        self.status = QLabel("设置已保存")
        self.status.setProperty("role", "muted")
        self.status.setWordWrap(True)
        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        self.save_button = QPushButton("保存设置")
        self.save_button.setProperty("role", "primary")
        self.save_button.setMinimumWidth(116)
        self.save_button.clicked.connect(lambda: self.saveRequested.emit())
        footer.addWidget(self.save_button)
        layout.addLayout(footer)
        for widget in self.fields.values():
            if isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(lambda *_: self.changed.emit())
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(lambda *_: self.changed.emit())
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(lambda *_: self.changed.emit())
            else:
                widget.textChanged.connect(lambda *_: self.changed.emit())
        self.elearning_editor.changed.connect(lambda *_: self.changed.emit())

    def add_section(self, label, *, form=True):
        page = QWidget()
        layout = QFormLayout(page) if form else QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 22)
        if form:
            layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            layout.setVerticalSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.sections.addTab(scroll, label)
        return page, layout

    def import_config(self, filename=None):
        if filename is None:
            filename, _ = QFileDialog.getOpenFileName(self, "导入 eLearning 配置", "", "JSON (*.json)")
        if not filename:
            return
        try:
            data = editable_config(filename)
        except (OSError, ValueError, TypeError, KeyError):
            self.status.setText("无法读取课程配置，原设置未改变。")
            return
        self.elearning_editor.set_data(data)
        self.status.setText("配置已导入，点击保存设置后生效。")

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
        # Old saved force flags must never silently re-bill a normal desktop run.
        values.update(overwrite=False, redo_notes=False, local_media="")
        for name, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                values[name] = widget.currentText().strip() if widget.isEditable() else widget.currentData()
            elif isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            else:
                values[name] = widget.text() if name in SECRET_FIELDS else widget.text().strip()
        values["elearning"] = self.elearning_editor.collect()
        return values

    def reset_note_budget(self):
        self.fields["llm_output_tokens"].setValue(DEFAULT_OUTPUT_TOKENS)
        self.fields["llm_timeout_minutes"].setValue(DEFAULT_TIMEOUT_MINUTES)
