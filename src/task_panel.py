"""Course/lecture progress and bounded, redacted diagnostics for the desktop."""

import logging
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .artifacts import atomic_write_text
from .task_events import redact
from .task_view_model import (
    STAGE_NAMES,
    STAGES,
    TaskViewModel,
    duration,
    size,
    stage_text,
)


def append_log(widget, text):
    """Follow new entries only while the user is already at the bottom."""
    bar = widget.verticalScrollBar()
    previous = bar.value()
    follow = previous >= bar.maximum() - 3
    widget.appendPlainText(text)
    bar.setValue(bar.maximum() if follow else previous)


class TaskPanel(QWidget):
    def __init__(self, parent=None, log_dir=None):
        super().__init__(parent)
        self.model = None
        self.items = {}
        self.groups = {}
        self.dirty = False
        self.diagnostics = deque(maxlen=2000)
        self.record_cursor = 0
        self.persisted_cursor = 0
        self.handler = None
        self.log_dir = Path(log_dir) if log_dir else None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.status = QLabel("就绪 · 选择课程后开始任务")
        self.status.setStyleSheet("font-weight:600; font-size:16px")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.counts = QLabel("按课程查看各课次的下载、转录和笔记状态。")
        self.counts.setWordWrap(True)
        layout.addWidget(self.counts)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["课程 / 课次", "下载", "本机转录", "笔记"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setStyleSheet("QTreeView::item { padding: 5px 4px; }")
        self.tree.setMinimumHeight(150)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3):
            self.tree.setColumnWidth(col, 150)
        self.tree.currentItemChanged.connect(lambda *_: self.render_detail())
        self.tree.currentItemChanged.connect(lambda *_: self.filter_changed() if self.log_filter.currentIndex() else None)
        layout.addWidget(self.tree, 3)
        self.detail = QLabel("选择一节课查看详情。")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)
        actions = QHBoxLayout()
        self.open_video = QPushButton("打开录像")
        self.open_note = QPushButton("打开笔记")
        self.open_video.clicked.connect(lambda: self.open_artifact("dl"))
        self.open_note.clicked.connect(lambda: self.open_artifact("sm"))
        self.copy_errors = QPushButton("复制问题摘要")
        self.copy_errors.clicked.connect(self.copy_error_summary)
        self.export = QPushButton("导出诊断…")
        self.export.clicked.connect(self.export_diagnostics)
        self.toggle_diagnostics = QPushButton("诊断详情")
        self.toggle_diagnostics.setCheckable(True)
        self.toggle_diagnostics.toggled.connect(self.show_diagnostics)
        for button in (self.open_video, self.open_note, self.copy_errors, self.export, self.toggle_diagnostics):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        log_controls = QHBoxLayout()
        self.log_filter = QComboBox()
        self.log_filter.addItems(["所有课次记录", "所选课次记录"])
        self.log_filter.currentIndexChanged.connect(self.filter_changed)
        log_controls.addWidget(self.log_filter)
        latest = QPushButton("回到最新")
        latest.clicked.connect(lambda: self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum()))
        log_controls.addWidget(latest)
        self.previous = QPushButton("上次运行结果")
        self.previous.clicked.connect(self.show_previous)
        self.previous.setEnabled(bool(self.log_dir and (self.log_dir / "last-run.txt").is_file()))
        log_controls.addWidget(self.previous)
        log_controls.addStretch()
        layout.addLayout(log_controls)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setPlaceholderText("关键变化会显示在这里；进度刷新不会重复刷屏。")
        self.log.setMaximumHeight(145)
        layout.addWidget(self.log, 1)
        self.debug = QPlainTextEdit()
        self.debug.setReadOnly(True)
        self.debug.setMaximumBlockCount(2000)
        self.debug.setMaximumHeight(180)
        self.debug.hide()
        layout.addWidget(self.debug, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.render)
        self.timer.start()
        self.render_detail()

    def begin(self, run_id):
        self.model = TaskViewModel(run_id)
        self.items.clear()
        self.groups.clear()
        self.tree.clear()
        self.log.clear()
        self.debug.clear()
        self.diagnostics.clear()
        self.record_cursor = 0
        self.persisted_cursor = 0
        self.close_log()
        if self.log_dir:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
                self.log_dir.chmod(0o700)
                for path in self.log_dir.glob("tasks.log*"):
                    if path.is_file() and time.time() - path.stat().st_mtime > 7 * 86400:
                        path.unlink()
                self.handler = RotatingFileHandler(self.log_dir / "tasks.log", maxBytes=2_000_000,
                                                  backupCount=4, encoding="utf-8")
                self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                self.save_result("任务已启动，尚未记录结束结果。若 App 意外退出，请重新开始任务以检查并复用已有文件。")
            except OSError:
                self.diagnostic("无法保存诊断日志；仍可在此窗口查看和导出。")
        self.dirty = True
        self.render()

    def close_log(self):
        if self.handler:
            self.handler.close()
            self.handler = None

    def save_result(self, text=None):
        if not self.log_dir:
            return
        try:
            value = text or f"{self.model.heading}\n{self.counts.text()}\n\n{self.error_summary()}"
            atomic_write_text(self.log_dir / "last-run.txt", time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + redact(value, export=True)[:100000])
            self.previous.setEnabled(True)
        except OSError:
            self.diagnostic("上次运行摘要暂时无法保存。")

    def show_previous(self):
        if self.log_dir:
            try:
                text = (self.log_dir / "last-run.txt").read_text(encoding="utf-8")[:100000]
                dialog = QMessageBox(self)
                dialog.setWindowTitle("最近一次运行记录")
                dialog.setText("这是保存的运行摘要，不代表后台仍在执行。")
                dialog.setDetailedText(text)
                dialog.exec()
            except OSError as exc:
                QMessageBox.warning(self, "无法读取运行记录", str(exc))

    def filter_changed(self, *_):
        self.log.clear()
        self.record_cursor = 0
        self.render_records()

    def render_records(self):
        if not self.model:
            return
        selected = self.selected_task()
        for record in self.model.records:
            if record["id"] > self.record_cursor:
                matches = selected and record["course_id"] == selected.course_id and record["sub_id"] == selected.sub_id
                if not self.log_filter.currentIndex() or matches:
                    append_log(self.log, f"{record['time']}  {record['message']}")
                self.record_cursor = record["id"]
            if record["id"] > self.persisted_cursor:
                self.persisted_cursor = record["id"]
                self.diagnostic(f"[关键变化] {record['message']}")

    def apply_event(self, value):
        if not self.model:
            return False
        try:
            changed = self.model.apply(value)
        except (KeyError, TypeError, ValueError, OverflowError):
            self.diagnostic("忽略一条格式不完整的进度事件。")
            return False
        self.dirty |= changed
        return changed

    def diagnostic(self, text):
        text = redact(text, export=True)[:16000].strip()
        if not text:
            return
        self.diagnostics.append(text)
        if self.debug.isVisible():
            append_log(self.debug, text)
        if self.handler:
            try:
                self.handler.emit(logging.LogRecord("icourse", logging.INFO, "", 0, text, (), None))
            except OSError:
                self.close_log()

    def show_diagnostics(self, checked):
        self.debug.setVisible(checked)
        if checked:
            self.debug.setPlainText("\n".join(self.diagnostics))
            self.debug.verticalScrollBar().setValue(self.debug.verticalScrollBar().maximum())

    def selected_task(self):
        item = self.tree.currentItem()
        key = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return self.model.tasks.get(tuple(key)) if self.model and key else None

    def render(self):
        model = self.model
        if model is None:
            return
        self.status.setText(model.heading)
        c = model.counts()
        ended = sum(c[s] for s in ("success", "failed", "pending", "cancelled", "interrupted"))
        detail = f"本次已结束 {ended}/{model.total} 课 · 成功 {c['success']} · 处理中 {c['running']} · 排队 {c['queued']}"
        for key, label in (("failed", "失败"), ("pending", "等待回放"), ("cancelled", "停止"), ("interrupted", "中断")):
            if c[key]:
                detail += f" · {label} {c[key]}"
        self.counts.setText(detail + f" · 用时 {duration(model.elapsed)}")
        if not model.planned and not model.exited:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, max(1, model.total))
            self.progress.setValue(ended)
            self.progress.setFormat(f"已结束 {ended}/{model.total} 课" if model.total else "无课次任务")
        for course_id, title in model.courses.items():
            if course_id not in self.groups:
                item = QTreeWidgetItem(self.tree)
                item.setFirstColumnSpanned(True)
                item.setExpanded(True)
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
                self.groups[course_id] = item
            cc = model.counts(course_id)
            count = sum(cc.values())
            suffix = f"成功 {cc['success']}/{count}"
            for key, label in (("running", "处理中"), ("failed", "失败"), ("pending", "待回放"), ("cancelled", "停止"), ("interrupted", "中断")):
                if cc[key]:
                    suffix += f" · {label} {cc[key]}"
            self.groups[course_id].setText(0, f"{title}  ({course_id})    {suffix}")
        for key, task in model.tasks.items():
            if key not in self.items:
                item = QTreeWidgetItem(self.groups[task.course_id])
                item.setData(0, Qt.ItemDataRole.UserRole, key)
                self.items[key] = item
            item = self.items[key]
            item.setText(0, task.title)
            item.setToolTip(0, f"{task.title} · 课次 {task.sub_id}")
            for col, name in enumerate(STAGES, 1):
                stage = task.stages[name]
                text = "未执行" if task.outcome in {"failed", "pending"} and stage.status in {"waiting", "queued"} else stage_text(stage)
                item.setText(col, text)
                item.setToolTip(col, stage.message or text)
        self.render_records()
        if self.tree.currentItem() is None and self.items:
            active = next((key for key, task in model.tasks.items() if task.outcome == "running"), next(iter(self.items)))
            self.tree.setCurrentItem(self.items[active])
        self.copy_errors.setEnabled(bool(c["failed"] or c["interrupted"] or model.final == "failed"))
        self.render_detail()
        self.dirty = False

    def render_detail(self):
        task = self.selected_task()
        for name, button in (("dl", self.open_video), ("sm", self.open_note)):
            button.setEnabled(bool(task and task.stages[name].status in {"done", "cached"} and task.paths.get(name)))
        if not task:
            self.detail.setText("选择一节课查看详情。")
            return
        lines = [f"{task.course_title} · {task.title}"]
        for name in STAGES:
            stage = task.stages[name]
            if stage.status not in {"running", "failed", "interrupted"}:
                continue
            metrics = stage.metrics
            text = f"{STAGE_NAMES[name]}：{stage.message or stage_text(stage)}"
            if metrics.get("unit") in {"bytes", "seconds"}:
                fmt = size if metrics["unit"] == "bytes" else duration
                text += f" · {fmt(metrics.get('completed'))} / {fmt(metrics.get('total'))}"
                if metrics.get("speed"):
                    text += f" · {size(metrics['speed'])}/s"
            if metrics.get("model"):
                text += f" · {metrics.get('provider', '')}/{metrics['model']}".replace("· /", "· ")
            if metrics.get("input_chars"):
                text += f" · 完整原文 {metrics['input_chars']} 字符"
            if metrics.get("attempt"):
                text += f" · 第 {metrics['attempt']}/{metrics.get('max_attempts', 1)} 次请求"
            if metrics.get("retry_seconds") and stage.status == "running":
                remaining = max(0, int(metrics['retry_seconds'] - (time.monotonic() - stage.changed)))
                text += f" · {remaining} 秒后重试"
            if stage.status == "running" and stage.started:
                text += f" · 已用时 {duration(time.monotonic() - stage.started)}"
            lines.append(text)
        if self.model and not self.model.exited and self.model.heartbeat and time.monotonic() - self.model.heartbeat > 20:
            lines.append("后台状态暂未更新，正在等待进程响应。")
        if self.toggle_diagnostics.isChecked() and self.model and self.model.engine_pid:
            lines.append(f"后台进程 PID：{self.model.engine_pid} · 课程 {task.course_id} / 课次 {task.sub_id}")
        self.detail.setText("\n".join(lines))

    def open_artifact(self, name):
        task = self.selected_task()
        if task and task.paths.get(name):
            path = Path(task.paths[name])
            if not path.is_file():
                QMessageBox.information(self, "文件暂不可用", "请检查保存磁盘是否已连接，以及文件是否仍在原位置。")
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def error_summary(self):
        lines = []
        if self.model:
            for task in self.model.tasks.values():
                for name, stage in task.stages.items():
                    if stage.status in {"failed", "interrupted"}:
                        lines.append(f"{task.course_title} [{task.course_id}/{task.sub_id}] · {task.title} · {STAGE_NAMES[name]}\n{stage.message or stage_text(stage)}")
        if not lines and self.model and self.model.final in {"failed", "interrupted"}:
            lines = [r["message"] for r in self.model.records[-5:]]
        return redact("\n\n".join(lines) or "没有失败课次", export=True)

    def copy_error_summary(self):
        QApplication.clipboard().setText(self.error_summary())

    def export_diagnostics(self):
        filename, _ = QFileDialog.getSaveFileName(self, "导出诊断", "iCourse-diagnostics.txt", "文本 (*.txt)")
        if filename:
            try:
                Path(filename).write_text(self.error_summary() + "\n\n" + "\n".join(self.diagnostics), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "无法导出", str(exc))
