import json
import os
import sys
import time
from uuid import uuid4

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QProcess, QTimer
from PySide6.QtWidgets import QApplication

from src.mac_gui import MainWindow
from src.preferences import defaults
from src.task_events import set_secrets
from src.task_panel import TaskPanel, append_log
from src.task_view_model import TaskViewModel


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def send(panel, kind, **data):
    event = dict(event="icourse", version=1, run_id=panel.model.run_id, seq=panel.model.seq + 1,
                 kind=kind, time="12:00:00", **data)
    assert panel.apply_event(event)


def plan(panel, course, status):
    send(panel, "task", course_id=course, course_title=f"课程 {course}", sub_id="123456", title="同名课次",
         stages=dict(dl="cached", tr="cached", sm=status))


def test_tree_groups_failure_counts_and_safe_diagnostics(app, tmp_path):
    panel = TaskPanel(log_dir=tmp_path)
    panel.begin("test")
    plan(panel, "101", "cached")
    plan(panel, "102", "queued")
    send(panel, "planned", total=2)
    send(panel, "stage", course_id="102", sub_id="123456", stage="sm", status="running")
    send(panel, "stage", course_id="102", sub_id="123456", stage="sm", status="failed", message="请求超时")
    send(panel, "run_finished", status="failed")
    panel.model.finish(1)
    panel.render()
    assert panel.tree.topLevelItemCount() == 2
    assert all(panel.tree.topLevelItem(i).childCount() == 1 for i in range(2))
    assert "成功 1" in panel.counts.text() and "失败 1" in panel.counts.text()
    assert panel.progress.value() == 2
    assert "102/123456" in panel.error_summary() and "请求超时" in panel.error_summary()
    secret = uuid4().hex
    set_secrets([secret])
    panel.diagnostic(f"API_KEY={secret}")
    panel.close_log()
    assert secret not in (tmp_path / "tasks.log").read_text()
    panel.close()


def test_log_scroll_stays_put_when_reading_history(app):
    panel = TaskPanel()
    panel.show()
    for i in range(100):
        append_log(panel.log, f"line {i}")
    app.processEvents()
    bar = panel.log.verticalScrollBar()
    bar.setValue(5)
    append_log(panel.log, "new line")
    assert bar.value() == 5
    panel.close()


def test_log_rotation_retention_and_previous_result(app, tmp_path):
    old = tmp_path / "tasks.log.4"
    old.write_text("old diagnostic")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    panel = TaskPanel(log_dir=tmp_path)
    panel.begin("test")
    assert not old.exists()
    panel.handler.maxBytes = 200
    panel.handler.backupCount = 2
    for i in range(100):
        panel.diagnostic(f"record {i}: " + "x" * 50)
    panel.model.final = "done"
    panel.model.finish(0)
    panel.render()
    panel.save_result()
    panel.close_log()
    assert len(list(tmp_path.glob("tasks.log*"))) <= 3
    assert (tmp_path / "last-run.txt").stat().st_size < 100000
    restored = TaskPanel(log_dir=tmp_path)
    assert restored.previous.isEnabled()
    assert restored.model is None  # Historical records never resurrect a running job.
    panel.close()
    restored.close()


def test_failed_retry_keeps_exact_pairs_and_reuses_upstream(app, monkeypatch, tmp_path):
    values = defaults()
    values.update(mode="summarize", course_ids="101,102", out_dir=str(tmp_path / "videos"),
                  summary_dir=str(tmp_path / "notes"), llm_models_1="test", llm_api_key_1=uuid4().hex,
                  llm_base_url_1="https://example.invalid", overwrite=True)
    window = MainWindow(initial_values=values)
    window.run_values = dict(values)
    window.panel.begin("old")
    plan(window.panel, "101", "cached")
    plan(window.panel, "102", "queued")
    send(window.panel, "stage", course_id="102", sub_id="123456", stage="sm", status="failed")
    launched = []
    monkeypatch.setattr(window.process, "start", lambda program, args: launched.append(args))
    monkeypatch.setattr("src.mac_gui.check_pipeline_storage", lambda *a, **k: None)
    window.retry_failed()
    args = launched[0]
    assert args[args.index("--target") + 1] == "102:123456"
    assert args[args.index("--resume-stage") + 1] == "102:123456:sm"
    assert "--overwrite" not in args and "--redo-notes" not in args
    assert args[args.index("--course-ids") + 1] == "102"
    assert window.process.processEnvironment().value("ICOURSE_RUN_ID") != "old"
    window.finished(1)
    window.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_child_exit_and_stop_preserve_completed_jobs(app, cancel):
    window = MainWindow(initial_values=defaults())
    window.panel.begin(uuid4().hex)
    plan(window.panel, "101", "cached")
    plan(window.panel, "102", "queued")
    window.start.setEnabled(False)
    window.stop.setEnabled(True)
    # Synthetic supervisor only: no school login, ASR or API invocation.
    window.process.start(sys.executable, ["-c", "import os,time; os.setsid(); time.sleep(20)" if cancel else "raise SystemExit(3)"])
    assert window.process.waitForStarted(5000)
    if cancel:
        QTimer.singleShot(50, window.cancel)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.start(7000)
    while deadline.isActive() and (window.process.state() != QProcess.ProcessState.NotRunning or window.group_pid):
        app.processEvents()
        window.process.waitForFinished(20)
    assert window.process.state() == QProcess.ProcessState.NotRunning and not window.group_pid
    assert window.panel.model.counts()["success"] == 1
    assert window.panel.model.counts()["cancelled" if cancel else "interrupted"] == 1
    assert window.start.isEnabled()
    window.close()


def test_invalid_or_old_events_do_not_replace_current_run(app):
    window = MainWindow(initial_values=defaults())
    window.panel.begin("new")
    window.handle_line(json.dumps(dict(event="icourse", version=1, run_id="old", seq=100, kind="run_finished", status="done")))
    assert window.panel.model.final is None
    window.handle_line(json.dumps(dict(event="icourse", version=1, run_id="new", seq=1, kind="planned", total="invalid")))
    assert isinstance(window.panel.model, TaskViewModel)
    assert window.panel.diagnostics
    window.close()
