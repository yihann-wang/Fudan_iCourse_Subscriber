"""Course dashboard; manual actions submit the current settings form credentials."""
import codecs
import contextlib
import io
import json
import os
import re
import signal
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

COMPLETED = {"submitted", "pending_review", "graded", "excused"}
SUBMISSION_LABELS = {"unsubmitted": "未提交", "submitted": "已提交", "pending_review": "已提交，待评阅",
    "graded": "已评分", "excused": "免交", "unknown": "提交状态待确认", "no_online_submission": "无需在线提交"}


def due_text(value):
    if not value:
        return "未设置截止时间"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return "截止时间待确认"


class ElearningPanel(QWidget):
    runningChanged = Signal(bool)
    settingsRequested = Signal()

    def __init__(self, parent=None, *, credential_provider=None, config_store=None):
        super().__init__(parent)
        self.credential_provider = credential_provider
        from .elearning_settings import personal_config_path
        self.config_store = Path(config_store) if config_store is not None else personal_config_path()
        self.pending_credentials = None
        self.operation = "check"
        self.results = []
        self.stdout_text = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.error_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_errors)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.started.connect(self.send_credentials)
        layout = QVBoxLayout(self)

        account_row = QHBoxLayout()
        account_note = QLabel("点击刷新或同步，将使用「设置」中的学号和密码登录复旦 eLearning。")
        account_note.setWordWrap(True)
        account_row.addWidget(account_note, 1)
        self.settings_button = QPushButton("前往设置")
        self.settings_button.clicked.connect(self.settingsRequested.emit)
        self.settings_button.setEnabled(credential_provider is not None)
        account_row.addWidget(self.settings_button)
        layout.addLayout(account_row)

        task_row = QHBoxLayout()
        title = QLabel("课程作业")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        task_row.addWidget(title)
        self.task_summary = QLabel("尚未刷新")
        task_row.addWidget(self.task_summary, 1)
        self.task_filter = QComboBox()
        for label, value in [("当前待办", "pending"), ("全部作业", "all"), ("已提交 / 已完成", "completed")]:
            self.task_filter.addItem(label, value)
        self.task_filter.currentIndexChanged.connect(self.render_tasks)
        task_row.addWidget(self.task_filter)
        self.refresh_button = QPushButton("刷新作业")
        self.refresh_button.clicked.connect(lambda: self.launch("check"))
        task_row.addWidget(self.refresh_button)
        layout.addLayout(task_row)
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["课程 / 作业", "截止时间（北京时间）", "提交 / 开放状态", "官方入口"])
        self.task_tree.setAlternatingRowColors(True)
        self.task_tree.setWordWrap(True)
        self.task_tree.setMinimumHeight(220)
        header = self.task_tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.task_tree.setColumnWidth(3, 82)
        layout.addWidget(self.task_tree, 1)

        file_row = QHBoxLayout()
        file_title = QLabel("课程文件")
        file_title.setStyleSheet("font-weight: 600;")
        file_row.addWidget(file_title)
        self.file_summary = QLabel("尚未同步")
        self.file_summary.setWordWrap(True)
        file_row.addWidget(self.file_summary, 1)
        self.sync_button = QPushButton("同步文件")
        self.sync_button.clicked.connect(lambda: self.launch("sync"))
        file_row.addWidget(self.sync_button)
        layout.addLayout(file_row)
        config_actions = QHBoxLayout()
        self.save_location = QLabel()
        self.save_location.setWordWrap(True)
        self.save_location.setTextFormat(Qt.TextFormat.PlainText)
        config_actions.addWidget(self.save_location, 1)
        self.edit_config = QPushButton('保存位置与课程设置…')
        self.edit_config.clicked.connect(self.configure)
        config_actions.addWidget(self.edit_config)
        layout.addLayout(config_actions)
        self.policy = QLabel()
        self.policy.setWordWrap(True)
        layout.addWidget(self.policy)
        status_row = QHBoxLayout()
        self.status = QLabel("点击刷新作业或同步文件开始。仅手动运行。")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        status_row.addWidget(self.status, 1)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        status_row.addWidget(self.stop_button)
        layout.addLayout(status_row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.details_toggle = QToolButton()
        self.details_toggle.setText("详细日志与选项")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(self.details_toggle)
        self.details = QWidget()
        detail_layout = QVBoxLayout(self.details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        config_row = QHBoxLayout()
        config_row.addWidget(QLabel("课程配置"))
        selected = self.config_store if self.config_store.is_file() else Path(__file__).parent / 'elearning_helper/config.json'
        self.config_path = QLineEdit(str(selected))
        self.config_path.setReadOnly(True)
        self.config_path.textChanged.connect(self.refresh_config_description)
        config_row.addWidget(self.config_path, 1)
        self.browse = QPushButton("导入配置…")
        self.browse.clicked.connect(self.choose_config)
        config_row.addWidget(self.browse)
        detail_layout.addLayout(config_row)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(3000)
        self.output.setMaximumHeight(180)
        detail_layout.addWidget(self.output)
        self.details.hide()
        self.details_toggle.toggled.connect(self.show_details)
        layout.addWidget(self.details)
        self.refresh_config_description()

    def show_details(self, visible):
        self.details.setVisible(visible)
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    def choose_config(self):
        filename, _ = QFileDialog.getOpenFileName(self, "选择 eLearning 配置", self.config_path.text(), "JSON (*.json)")
        if filename:
            self.configure(filename)

    def configure(self, filename=None):
        if self.is_running():
            return
        from .elearning_settings import ElearningSettingsDialog
        try:
            dialog = ElearningSettingsDialog(filename if isinstance(filename, str) else self.config_path.text(),
                                             self.config_store, self)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.status.setText(f'无法读取课程配置：{exc}')
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config_path.setText(str(self.config_store))
            self.refresh_config_description()
            self.status.setText('配置已保存，下次刷新或同步时使用；已有文件保持原位置。')

    def refresh_config_description(self):
        from .elearning_helper.config import load_config
        try:
            config = load_config(self.config_path.text())
            self.save_location.setText(f'保存位置：{config.root} · {len(config.courses)} 门课程')
            self.policy.setText(f'所有可访问类型 · 单文件 < {config.max_bytes:,} 字节 · 仅保存目录内查重 · 同名直接保留')
        except (OSError, ValueError, KeyError, TypeError):
            self.save_location.setText('课程配置无法读取，请导入有效配置。')
            self.policy.setText('同名文件保留；设置后手动开始同步。')

    def is_running(self):
        return self.process.state() != QProcess.ProcessState.NotRunning

    def needs_login(self):
        return self.operation in {"sync", "check"}

    def command(self):
        auth = ["--login-stdin"] if self.needs_login() else []
        return ["-m", "src.elearning_helper", "--config", self.config_path.text(), "--json", *auth, self.operation]

    def prepare_credentials(self):
        if self.credential_provider is None:
            self.status.setText("请从 Subscriber 主窗口使用此功能；独立测试入口不读取统一设置。")
            return False
        values = None
        try:
            values = self.credential_provider()
            if (not isinstance(values, dict) or set(values) != {"student_id", "password"}
                    or not all(isinstance(value, str) and value for value in values.values())):
                self.status.setText("请先在「设置」填写学号和密码，再刷新作业或同步文件。")
                return False
            payload = json.dumps(values).encode("utf-8")
            if len(payload) > 8192:
                self.status.setText("登录输入过长，未发起登录。")
                return False
            self.pending_credentials = payload
            return True
        except Exception:
            self.status.setText("无法取得统一登录信息，未发起登录；请检查「设置」。")
            return False
        finally:
            if isinstance(values, dict):
                values.clear()  # Provider returns a temporary copy of two fields.

    def launch(self, operation=None):
        if self.is_running():
            return
        if operation is not None:
            self.operation = operation
        if self.operation not in {"check", "sync", "demo", "doctor"}:
            return
        if self.operation != "demo" and not Path(self.config_path.text()).is_file():
            self.status.setText("课程配置文件不存在，请在详细选项中选择配置。")
            return
        if self.needs_login() and not self.prepare_credentials():
            return
        self.decoder.reset()
        self.error_decoder.reset()
        self.stdout_text = ""
        self.output.clear()
        environment = QProcessEnvironment.systemEnvironment()
        for key in ("StuId", "STUID", "UISPsw", "FUDAN_ELEARNING_TOKEN"):
            environment.remove(key)
        environment.insert("PYTHONUNBUFFERED", "1")
        environment.insert("PYTHONDONTWRITEBYTECODE", "1")
        self.process.setProcessEnvironment(environment)
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parent.parent))
        self.set_running(True)
        self.status.setText("正在同步文件并刷新作业…" if self.operation == "sync" else "正在读取作业…" if self.operation == "check" else "正在运行离线检查…")
        self.process.start(sys.executable, self.command())

    def send_credentials(self):
        if self.pending_credentials is not None:
            self.process.write(self.pending_credentials)
            self.pending_credentials = None
        self.process.closeWriteChannel()

    def set_running(self, value):
        for widget in (self.refresh_button, self.sync_button, self.config_path, self.browse, self.edit_config):
            widget.setEnabled(not value)
        self.settings_button.setEnabled(not value and self.credential_provider is not None)
        self.stop_button.setEnabled(value)
        self.progress.setVisible(value)
        self.runningChanged.emit(value)

    def read_output(self):
        self.stdout_text += self.decoder.decode(bytes(self.process.readAllStandardOutput()))

    def read_errors(self):
        text = self.error_decoder.decode(bytes(self.process.readAllStandardError()))
        if text:
            self.output.appendPlainText(text.rstrip())

    def apply_payload(self, payload):
        from .elearning_helper.__main__ import display
        if "results" in payload:
            self.results = payload["results"]
            groups = [self.results]
        elif "third_sync" in payload:
            self.results = payload["third_sync"]
            groups = [payload[name] for name in ("first_sync", "second_sync", "third_sync")]
            self.output.appendPlainText(payload.get("note", "离线模拟数据"))
        else:
            self.output.appendPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        with contextlib.redirect_stdout(io.StringIO()) as details:
            for results in groups:
                display(results, "Asia/Shanghai")
        self.output.appendPlainText(details.getvalue().rstrip())
        self.render_tasks()
        files = [item for course in self.results for item in course.get("files", [])]
        if files or self.operation == "sync":
            counts = Counter(item["status"] for item in files)
            labels = {"downloaded": "新增", "duplicate": "复用", "local_changed": "保留本地修改",
                      "name_preserved": "同名保留", "failed": "失败", "skipped": "跳过"}
            self.file_summary.setText(" · ".join(f"{label} {counts[key]}" for key, label in labels.items() if counts[key]) or "本次没有可同步文件")

    def render_tasks(self):
        self.task_tree.clear()
        pending = completed = failures = 0
        selection = self.task_filter.currentData()
        now = datetime.now(timezone.utc)
        for course in self.results:
            checked = course.get("assignments")
            # Display filtering only: retain every assignment in results and the
            # backend baseline so a later refresh can reveal unlocked work.
            items = [item for item in checked.get("assignments", [])
                     if not item.get("locked_for_user")] if checked else []
            done = sum(item.get("submission") in COMPLETED for item in items)
            todo = len(items) - done
            pending += todo; completed += done
            visible = [item for item in items if selection == "all" or
                       (item.get("submission") in COMPLETED) == (selection == "completed")]
            if checked and not visible:
                continue
            parent = QTreeWidgetItem(self.task_tree, [f"{course['course']}  ·  待办 {todo} / 已完成 {done}"])
            font = parent.font(0); font.setBold(True); parent.setFont(0, font)
            parent.setExpanded(True)
            if not checked:
                failures += 1
                QTreeWidgetItem(parent, ["本次未能读取作业，请查看详细日志"])
                continue
            visible.sort(key=lambda item: (item.get("submission") in COMPLETED, item.get("due_at") or "9999", item.get("title", "")))
            for item in visible:
                submission = item.get("submission", "unknown")
                label = SUBMISSION_LABELS.get(submission, "提交状态待确认")
                overdue = False
                try:
                    overdue = submission == "unsubmitted" and bool(item.get("due_at")) and datetime.fromisoformat(item["due_at"].replace("Z", "+00:00")) < now
                except (ValueError, TypeError):
                    pass
                if overdue:
                    label += " · 已逾期"
                child = QTreeWidgetItem(parent, [item["title"], due_text(item.get("due_at")), label, ""])
                child.setToolTip(0, item["title"])
                if item.get("unlock_at"):
                    child.setToolTip(2, "接口开放时间：" + due_text(item["unlock_at"]))
                if submission in COMPLETED:
                    for column in range(3): child.setForeground(column, QColor("#777777"))
                elif overdue:
                    child.setForeground(2, QColor("#b45309"))
                url = item.get("url", "")
                if self.official_assignment_url(url):
                    button = QPushButton("打开")
                    button.setToolTip("在浏览器打开官方作业页面")
                    button.clicked.connect(lambda _checked=False, target=url: self.open_assignment(target))
                    self.task_tree.setItemWidget(child, 3, button)
        self.task_summary.setText(f"{pending} 项待办 · {completed} 项已完成" + (f" · {failures} 门读取失败" if failures else ""))

    @staticmethod
    def official_assignment_url(url):
        try:
            value = urlsplit(url)
            return (value.scheme == "https" and value.hostname == "elearning.fudan.edu.cn"
                    and value.port in (None, 443) and not value.username and not value.password
                    and not value.query and not value.fragment
                    and re.fullmatch(r"/courses/[0-9]+/assignments/[0-9]+/?", value.path) is not None)
        except (ValueError, TypeError):
            return False

    def open_assignment(self, url):
        if self.official_assignment_url(url):
            QDesktopServices.openUrl(QUrl(url))

    def finished(self, code, exit_status=QProcess.ExitStatus.NormalExit):
        self.pending_credentials = None
        self.read_output(); self.read_errors()
        parsed = False
        if self.stdout_text.strip():
            try:
                self.apply_payload(json.loads(self.stdout_text))
                parsed = True
            except (ValueError, TypeError, KeyError):
                self.output.appendPlainText(self.stdout_text.rstrip())
        if code == 0 and exit_status == QProcess.ExitStatus.NormalExit and parsed:
            label = "文件同步与作业刷新完成。" if self.operation == "sync" else "作业已刷新。" if self.operation == "check" else "离线检查完成，未连接学校。"
        elif code == 3:
            label = "登录或验证未完成，请检查统一设置；任务数据未完整更新，详见日志。"
        elif code == 130:
            label = "已停止；已完成文件保留。"
        else:
            label = "本次操作未全部完成，请展开详细日志。"
        self.status.setText(label)
        self.set_running(False)

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.pending_credentials = None
            self.status.setText("无法启动运行进程，请查看本地运行环境。")
            self.set_running(False)

    def stop(self):
        if self.is_running():
            try:
                os.kill(int(self.process.processId()), signal.SIGINT)
            except (OSError, ValueError):
                self.process.terminate()
            pid = int(self.process.processId())
            QTimer.singleShot(2000, lambda: self.kill_if_running(pid))

    def kill_if_running(self, pid):
        if self.is_running() and int(self.process.processId()) == pid:
            self.process.kill()


class ElearningWindow(QMainWindow):
    """Offline test entry; live settings are available only in Subscriber."""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("iCourse · eLearning 测试入口")
        self.resize(1080, 760)
        self.panel = ElearningPanel(self)
        self.setCentralWidget(self.panel)
        self.closing = False
        self.panel.runningChanged.connect(self.running_changed)

    def running_changed(self, running):
        if self.closing and not running: self.close()

    def closeEvent(self, event):
        if self.panel.is_running():
            self.closing = True; self.panel.stop(); event.ignore()
        else:
            self.panel.pending_credentials = None
            event.accept()


def main():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("iCourse eLearning")
    window = ElearningWindow(); window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
