"""Qt desktop shell over the same CLI/pipeline used in batch runs."""

from copy import deepcopy
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
    QApplication, QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QTabWidget, QVBoxLayout, QHBoxLayout, QWidget, QMenu, QToolButton, QFrame,
)

from .preferences import SECRET_FIELDS, Preferences, defaults, runtime_environment
from .storage_access import StorageAccessError, check_directory, check_pipeline_storage
from .task_events import redact, set_secrets
from .task_panel import TaskPanel
from .elearning_panel import ElearningPanel
from .elearning_settings import personal_config_path, editable_config, config_data
from .elearning_helper.config import parse_config
from .settings_panel import SettingsPanel
from .workspace_paths import check_parallel_paths
from .ui_style import CourseGlyph, ElidedLabel, apply_theme, short_path

ROOT = Path(__file__).resolve().parent.parent


class MainWindow(QMainWindow):
    def __init__(self, preferences=None, initial_values=None, elearning_config_store=None):
        super().__init__()
        self.preferences = preferences or Preferences()
        loaded = initial_values if initial_values is not None else self.preferences.load(ROOT / ".icourse_gui_config.json")
        self.values = deepcopy({**defaults(), **loaded})
        source = Path(elearning_config_store) if elearning_config_store is not None else personal_config_path()
        if not source.is_file() or (initial_values is not None and elearning_config_store is None):
            source = Path(__file__).parent / "elearning_helper/config.json"
        if self.values.get("elearning") is None:
            self.values["elearning"] = editable_config(source)
        else:
            self.values["elearning"] = config_data(parse_config(self.values["elearning"], source))
        self._video_busy = False
        self.dirty = False
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self.read_output)
        self.process.readyReadStandardError.connect(self.read_error)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.started.connect(self.process_started)
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
        self.setWindowTitle("iCourse · 课程与笔记")
        self.resize(1080, 760)
        self.setMinimumSize(860, 620)
        central = QWidget()
        central.setObjectName("appRoot")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(20)
        header = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(4)
        title = QLabel("iCourse")
        title.setProperty("role", "brand")
        brand.addWidget(title)
        subtitle = QLabel("课程、笔记与学习资料")
        subtitle.setProperty("role", "muted")
        brand.addWidget(subtitle)
        header.addLayout(brand)
        header.addStretch()
        self.activity = QLabel()
        self.activity.setProperty("role", "badge")
        header.addWidget(self.activity, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        self.draft_notice = QLabel("设置有未保存修改，保存后可启动新任务；运行中的任务不受影响。")
        self.draft_notice.setProperty("role", "notice")
        self.draft_notice.setWordWrap(True)
        layout.addWidget(self.draft_notice)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setDrawBase(False)
        layout.addWidget(self.tabs, 1)
        self.video_page = QWidget()
        video_layout = QVBoxLayout(self.video_page)
        video_layout.setContentsMargins(0, 8, 0, 0)
        video_layout.setSpacing(18)
        summary_card = QFrame()
        summary_card.setProperty("role", "card")
        summary_row = QHBoxLayout(summary_card)
        summary_row.setContentsMargins(20, 16, 20, 16)
        summary_row.setSpacing(16)
        summary_row.addWidget(CourseGlyph(small=True))
        summary_text = QVBoxLayout()
        summary_text.setSpacing(5)
        self.video_context = QLabel()
        self.video_context.setProperty("role", "muted")
        summary_text.addWidget(self.video_context)
        self.video_summary = QLabel()
        self.video_summary.setProperty("role", "sectionTitle")
        self.video_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.video_summary.setWordWrap(True)
        summary_text.addWidget(self.video_summary)
        self.video_location = ElidedLabel()
        self.video_location.setProperty("role", "muted")
        self.note_location = ElidedLabel()
        self.note_location.setProperty("role", "muted")
        summary_text.addWidget(self.video_location)
        summary_text.addWidget(self.note_location)
        summary_row.addLayout(summary_text, 1)
        video_settings = QPushButton("调整设置")
        video_settings.setProperty("role", "quiet")
        video_settings.clicked.connect(lambda: self.show_settings(1))
        summary_row.addWidget(video_settings)
        video_layout.addWidget(summary_card)
        self.video_index = self.tabs.addTab(self.video_page, "录像与笔记")
        self.settings = SettingsPanel(self.values, source)
        self.fields = self.settings.fields
        self.mode, self.mode_hint = self.settings.mode, self.settings.mode_hint
        self.values = self.collect()
        self.elearning = ElearningPanel(self, credential_provider=self.elearning_credentials,
                                       config_store=source, config_provider=lambda: deepcopy(self.values["elearning"]),
                                       before_launch=self.prepare_elearning)
        self.elearning.settingsRequested.connect(lambda: self.show_settings(2))
        self.elearning_index = self.tabs.addTab(self.elearning, "eLearning 与作业")
        self.elearning.runningChanged.connect(self.elearning_running_changed)
        self.settings_index = self.tabs.addTab(self.settings, "设置")
        self.settings.changed.connect(self.settings_changed)
        self.settings.saveRequested.connect(self.save)
        self.settings.checkRequested.connect(self.launch)
        log_dir = Path.home() / "Library/Logs/Fudan iCourse Subscriber" if preferences is None and initial_values is None else None
        self.panel = TaskPanel(self, log_dir=log_dir)
        self.status, self.progress, self.log = self.panel.status, self.panel.progress, self.panel.log
        self.panel.set_compact(True)
        self.panel.set_logs_visible(False)
        for button in (self.panel.previous, self.panel.copy_errors, self.panel.export, self.panel.toggle_diagnostics):
            button.hide()
        self.progress_scroll = QScrollArea()
        self.progress_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.progress_scroll.setWidgetResizable(True)
        self.progress_scroll.setWidget(self.panel)
        video_layout.addWidget(self.progress_scroll, 1)
        self.video_actions = QWidget()
        actions = QHBoxLayout(self.video_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        open_folder = QPushButton("打开保存位置")
        open_folder.setProperty("role", "quiet")
        open_folder.clicked.connect(self.open_output)
        actions.addWidget(open_folder)
        more = QToolButton()
        more.setText("更多操作")
        more.setProperty("role", "quiet")
        more.setMinimumWidth(108)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        self.retry = menu.addAction("仅重试失败课次")
        self.retry.triggered.connect(self.retry_failed)
        self.verify = menu.addAction("完整校验录像…")
        self.verify.triggered.connect(lambda: self.launch("verify-videos"))
        self.redownload = menu.addAction("重新下载指定录像…")
        self.redownload.triggered.connect(lambda: self.launch("redownload-videos"))
        menu.addSeparator()
        self.previous_action = menu.addAction("上次运行结果")
        self.previous_action.triggered.connect(self.panel.show_previous)
        self.show_logs = menu.addAction("显示任务日志")
        self.show_logs.setCheckable(True)
        self.show_logs.toggled.connect(self.panel.set_logs_visible)
        diagnostics = menu.addAction("显示诊断详情")
        diagnostics.setCheckable(True)
        diagnostics.toggled.connect(self.panel.toggle_diagnostics.setChecked)
        self.panel.toggle_diagnostics.toggled.connect(diagnostics.setChecked)
        menu.addAction("复制问题摘要").triggered.connect(self.panel.copy_error_summary)
        menu.addAction("导出诊断…").triggered.connect(self.panel.export_diagnostics)
        more.setMenu(menu)
        actions.addWidget(more)
        actions.addStretch()
        self.stop = QPushButton("停止录像任务")
        self.stop.clicked.connect(self.cancel)
        actions.addWidget(self.stop)
        self.start = QPushButton("开始任务")
        self.start.setProperty("role", "primary")
        self.start.setMinimumWidth(116)
        self.start.clicked.connect(lambda: self.launch("task"))
        actions.addWidget(self.start)
        video_layout.addWidget(self.video_actions)
        self.setCentralWidget(central)
        apply_theme(self)
        QApplication.styleHints().colorSchemeChanged.connect(self.update_appearance)
        self.refresh_controls()

    def update_appearance(self, scheme):
        apply_theme(self, scheme == Qt.ColorScheme.Dark)

    def video_is_running(self):
        return self._video_busy or self.process.state() != QProcess.ProcessState.NotRunning or bool(self.group_pid)

    def show_settings(self, section=0):
        self.tabs.setCurrentIndex(self.settings_index)
        self.settings.sections.setCurrentIndex(section)

    def elearning_running_changed(self, running):
        self.refresh_controls()
        if self.closing and not running:
            QTimer.singleShot(0, self.close)

    def elearning_credentials(self):
        return {"student_id": self.values["stu_id"].strip(), "password": self.values["uis_psw"]}

    def prepare_elearning(self):
        if self.dirty or self.closing:
            self.elearning.status.setText("请先保存设置，再启动新任务。" if self.dirty else "程序正在停止任务。")
            return False
        if self.video_is_running() and self.run_values and self.elearning.operation == 'sync':
            try:
                check_parallel_paths(self.run_values, self.values['elearning'])
            except ValueError as exc:
                self.elearning.status.setText(str(exc))
                return False
        return True

    def collect(self):
        return self.settings.collect()

    def settings_changed(self):
        try:
            self.dirty = self.collect() != self.values
        except (ValueError, TypeError, KeyError, OSError):
            self.dirty = True
        self.settings.status.setText("有未保存修改" if self.dirty else "设置已保存")
        self.refresh_controls()

    def refresh_controls(self):
        busy = self.video_is_running()
        ready = not busy and not self.dirty and not self.closing
        for button in (self.start, self.verify, self.redownload):
            button.setEnabled(ready)
        self.retry.setEnabled(ready and getattr(self, "run_action", "task") == "task" and
                              self.panel.model is not None and bool(self.panel.model.counts()["failed"]))
        self.stop.setEnabled(busy and not self.cancelled and not self.closing)
        self.stop.setVisible(busy)
        self.previous_action.setEnabled(self.panel.previous.isEnabled())
        self.settings.save_button.setEnabled(not self.closing)
        for button in self.settings.check_buttons:
            button.setEnabled(ready)
        self.settings.check_note.setVisible(busy)
        self.elearning.set_start_allowed(not self.dirty and not self.closing)
        self.draft_notice.setVisible(self.dirty)
        running = [name for name, active in (("录像与笔记", busy), ("eLearning", self.elearning.is_running())) if active]
        self.activity.setText(" · ".join(running) + "运行中" if running else "所有任务空闲")
        values = self.run_values if busy and self.run_values else self.values
        mode = "只下载录像" if values["mode"] == "download" else "下载录像并生成笔记"
        count = len([x for x in values["course_ids"].split(",") if x.strip()])
        self.video_context.setText("本次任务" if busy else "下次运行")
        self.video_summary.setText(f"{mode}  ·  {count} 门课程")
        same_path = Path(values['out_dir']).expanduser() == Path(values['summary_dir']).expanduser()
        self.video_location.setText(f"{'保存到' if same_path or values['mode'] == 'download' else '录像'}  {short_path(values['out_dir'])}")
        self.video_location.setToolTip(values['out_dir'])
        self.note_location.setText(f"笔记  {short_path(values['summary_dir'])}")
        self.note_location.setToolTip(values['summary_dir'])
        self.note_location.setVisible(not same_path and values['mode'] != 'download')
        self.panel.empty.title.setText("课程准备就绪" if count else "从添加课程开始")
        self.panel.empty.description.setText(
            "点击右下角「开始任务」，在这里查看各课次的下载与处理进度。\n已完成的内容会继续保留。" if count else
            "先在设置中填写课程 ID 和保存位置，再开始下载录像或生成笔记。")

    def save(self):
        try:
            values = self.collect()
            self.preferences.save(values)
        except ValueError as exc:
            self.settings.status.setText(f"设置未保存：{exc}")
            return False
        except Exception:
            self.settings.status.setText("设置未保存，请检查课程配置和保存位置；原配置保留。")
            return False
        self.values = deepcopy(values)
        self.settings.values = deepcopy(values)
        self.dirty = False
        self.settings.status.setText("设置已保存，下次启动使用；运行中的任务继续使用原配置。")
        self.elearning.refresh_config_description()
        self.refresh_controls()
        return True

    def reset_note_budget(self):
        self.settings.reset_note_budget()

    def command(self, action, values):
        if action == "redownload-videos":
            if not values.get("course_ids") or not values.get("sub_ids"):
                raise ValueError("请填写课程 ID 和指定课次；重新下载仅处理这些录像，保留原文和笔记。")
            repair = dict(values, mode="download", overwrite=False, redo_notes=False)
            return self.command("task", repair) + ["--redownload"]
        if action == "verify-videos":
            if not values["course_ids"]:
                raise ValueError("请填写要校验的课程 ID；可用指定课次缩小范围。")
            return [action, "--out-dir", values["out_dir"], "--course-ids", values["course_ids"],
                    "--sub-ids", values.get("sub_ids", "")]
        if action != "task":
            return [action]
        if values["mode"] not in {"download", "download_and_summarize"}:
            raise ValueError("请选择下载录像并生成笔记，或只下载录像。")
        if not values["course_ids"]:
            raise ValueError("请填写课程 ID。")
        if not (values["stu_id"] and values["uis_psw"]):
            raise ValueError("下载课程需要在设置中填写学号和密码。")
        if values["mode"] != "download" and not all(values[k] for k in ("llm_api_key_1", "llm_base_url_1", "llm_models_1")):
            raise ValueError("生成笔记需要填写服务地址、模型和 API Key。")
        return ["run", "--mode", values["mode"], "--course-ids", values["course_ids"],
                "--out-dir", values["out_dir"], "--summary-dir", values["summary_dir"],
                "--sub-ids", values["sub_ids"], "--skip-time-periods", values["skip_time_periods"]]

    def launch(self, action, retry_tasks=None):
        if self.video_is_running() or self.closing:
            return
        if self.dirty:
            self.settings.status.setText("请先保存设置，再启动新任务。")
            self.show_settings(1)
            return
        try:
            values = deepcopy(self.values)
            if action == "redownload-videos":
                values.update(mode="download", overwrite=False, redo_notes=False)
            if retry_tasks and self.run_values:
                # Preserve the run's input/output selection, but allow corrected credentials/models.
                for key in ("mode", "out_dir", "summary_dir", "local_media"):
                    values[key] = self.run_values[key]
                values.update(course_ids=",".join(dict.fromkeys(t.course_id for t in retry_tasks)),
                              sub_ids="", skip_time_periods="", overwrite=False, redo_notes=False)
            if action in {"task", "redownload-videos"}:
                for name in ("out_dir", "summary_dir"):
                    path = Path(values[name] or defaults()[name]).expanduser()
                    values[name] = str((path if path.is_absolute() else ROOT / path).resolve())
            args = self.command(action, values)
            if (self.elearning.is_running() and self.elearning.operation == 'sync'
                    and self.elearning.run_config and action in {'task', 'redownload-videos', 'verify-videos'}):
                check_parallel_paths(values, self.elearning.run_config)
            if action == "redownload-videos":
                action = "task"
            if retry_tasks:
                for task in retry_tasks:
                    stage = next(name for name, state in task.stages.items() if state.status == "failed")
                    args.extend(["--target", f"{task.course_id}:{task.sub_id}",
                                 "--resume-stage", f"{task.course_id}:{task.sub_id}:{stage}"])
            env = (runtime_environment(values) if action != "verify-videos" else
                   {k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "TMPDIR", "QT_QPA_PLATFORM", "ICOURSE_STATE_DIR"}})
            if action == "task":
                # Request access in the native GUI process. The supervised worker
                # repeats these checks before login or model work.
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
        self.run_action = action
        self.buffer = ""
        self.error_buffer = ""
        self.decoder.reset()
        self.error_decoder.reset()
        self.panel.begin(run_id)
        self.panel.toggle_diagnostics.setChecked(action in {"doctor", "check-asr"})
        self._video_busy = True
        self.refresh_controls()
        self.tabs.setCurrentIndex(self.video_index)
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

    def process_started(self):
        self.engine_pid = int(self.process.processId())
        if self.cancelled:
            self.stop_process_group()

    def cancel(self):
        if self.cancelled or self.process.state() == QProcess.ProcessState.NotRunning:
            return
        self.cancelled = True
        if self.panel.model:
            self.panel.model.stopping = True
            self.panel.render()
        self.stop.setEnabled(False)
        self.stop_process_group()

    def stop_process_group(self):
        pid = int(self.process.processId())
        if pid <= 0:
            # started will retry once QProcess has an actual child PID.
            return
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
        if self.group_pid != pid:
            return
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
            self.retry.setEnabled(getattr(self, "run_action", "task") == "task" and bool(self.panel.model.counts()["failed"]))
            self.panel.save_result()
        self._video_busy = False
        self.refresh_controls()
        self.panel.close_log()
        if self.closing:
            self.close()

    def open_output(self):
        values = self.run_values if self.video_is_running() and self.run_values else self.values
        folder = Path(values["out_dir"] if values["mode"] == "download" else values["summary_dir"])
        try:
            check_directory(folder, "保存位置", writable=True, create=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        except (StorageAccessError, OSError) as exc:
            QMessageBox.warning(self, "无法打开保存位置", str(exc))

    def closeEvent(self, event):
        if self.video_is_running() or self.elearning.is_running():
            self.closing = True
            self.refresh_controls()
            self.cancel()
            self.elearning.stop()
            event.ignore()
        else:
            self.panel.close_log()
            event.accept()


def main():
    if "--elearning" in sys.argv[1:]:
        # Dedicated entry never constructs Preferences or loads saved credentials.
        from .elearning_panel import main as elearning_main
        return elearning_main()
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
