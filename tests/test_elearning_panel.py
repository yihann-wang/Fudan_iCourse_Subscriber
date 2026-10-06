"""GUI adapter tests use fake values, offscreen Qt and offline demo only."""
import json
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from src import cli
from src.elearning_panel import ElearningPanel, ElearningWindow
from src.mac_gui import MainWindow
from src.preferences import defaults


@pytest.fixture
def app(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "question", lambda *_: pytest.fail("manual login must not ask permission"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def finish(app, panel):
    deadline = time.monotonic() + 20
    while panel.is_running() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    if panel.is_running():
        panel.process.kill()
        panel.process.waitForFinished(3000)
        pytest.fail("offline child did not finish")


def test_default_has_no_duplicate_login_inputs_or_automatic_operation(app):
    panel = ElearningPanel()
    assert panel.command()[-1] == "check" and "--json" in panel.command()
    assert not hasattr(panel, "student_id") and not hasattr(panel, "password")
    assert not panel.is_running()
    assert not panel.details_toggle.isChecked()
    assert Path(panel.config_path.text()).is_file()
    panel.close()


def test_main_window_exposes_tab_without_changing_video_settings(app):
    values = defaults()
    window = MainWindow(initial_values=values)
    assert window.tabs.tabText(window.elearning_index) == "eLearning 与作业"
    assert {k: v for k, v in window.collect().items() if k != "elearning"} == {k: v for k, v in values.items() if k != "elearning"}
    window.tabs.setCurrentIndex(window.elearning_index)
    assert not window.video_actions.isVisible()
    assert not window.progress_scroll.isVisible()
    window.tabs.setCurrentIndex(0)
    assert not window.video_actions.isHidden()
    window.close()


def test_offline_demo_runs_through_gui_process(app, monkeypatch):
    monkeypatch.delenv("FUDAN_ELEARNING_TOKEN", raising=False)
    panel = ElearningPanel()
    panel.launch("demo")
    finish(app, panel)
    assert panel.process.exitCode() == 0
    text = panel.output.toPlainText()
    assert "[新作业]" in text
    assert "[截止时间变更]" in text
    assert "未访问学校" in text
    assert panel.refresh_button.isEnabled()
    assert panel.task_tree.topLevelItemCount() == 1
    assert "复用" in panel.file_summary.text()
    panel.close()


def test_live_preview_without_auth_is_explicitly_incomplete(app, monkeypatch, tmp_path):
    monkeypatch.delenv("FUDAN_ELEARNING_TOKEN", raising=False)
    panel = ElearningPanel()
    config = json.loads(Path(panel.config_path.text()).read_text())
    config["state_dir"] = str(tmp_path / "state")
    filename = tmp_path / "config.json"
    filename.write_text(json.dumps(config))
    panel.config_path.setText(str(filename))
    panel.launch()
    finish(app, panel)
    assert panel.process.state() == QProcess.ProcessState.NotRunning
    assert not panel.process.program()
    assert "主窗口" in panel.status.text()
    assert not (tmp_path / "state").exists()
    panel.close()


def test_manual_settings_login_is_sent_only_over_private_pipe(app, monkeypatch):
    monkeypatch.setenv("UISPsw", "OLD_FIXTURE_NEVER_USED")
    monkeypatch.setenv("FUDAN_ELEARNING_TOKEN", "OLD_FIXTURE_TOKEN_NEVER_USED")
    events = []
    def provider():
        events.append("provider")
        return dict(student_id="TEST_STUDENT", password="TEST_PASSWORD_NOT_REAL")
    panel = ElearningPanel(credential_provider=provider)
    assert events == []
    script = ("import json,sys,os; d=json.load(sys.stdin); "
              "assert d==dict(student_id='TEST_STUDENT',password='TEST_PASSWORD_NOT_REAL'); "
              "assert 'UISPsw' not in os.environ and 'FUDAN_ELEARNING_TOKEN' not in os.environ; "
              "print(json.dumps(dict(results=[])))")
    monkeypatch.setattr(panel, "command", lambda: ["-c", script])
    panel.launch("check")
    finish(app, panel)
    assert panel.process.exitCode() == 0 and panel.pending_credentials is None
    assert events == ["provider"]
    assert "TEST_PASSWORD" not in panel.output.toPlainText() + panel.stdout_text
    panel.launch("check")
    finish(app, panel)
    assert events == ["provider", "provider"]
    panel.close()


def test_safe_window_does_not_load_preferences(app, monkeypatch):
    from src.preferences import Preferences
    monkeypatch.setattr(Preferences, "load", lambda *_: pytest.fail("must not load saved secrets"))
    monkeypatch.setattr(Preferences, "save", lambda *_: pytest.fail("must not persist secrets"))
    window = ElearningWindow()
    assert window.panel.credential_provider is None
    window.close()


def test_safe_mac_entry_does_not_construct_main_window(monkeypatch):
    from src import mac_gui, elearning_panel
    monkeypatch.setattr(mac_gui.sys, "argv", ["icourse", "--elearning"])
    monkeypatch.setattr(mac_gui, "MainWindow", lambda: pytest.fail("must not load old settings"))
    monkeypatch.setattr(elearning_panel, "main", lambda: 17)
    assert mac_gui.main() == 17


def test_cli_bridge_dispatches_without_loading_preferences(monkeypatch):
    from src.elearning_helper import __main__
    seen = []
    monkeypatch.setattr(__main__, "main", lambda args: seen.append(args) or 0)
    assert cli.main(["elearning", "sync", "--dry-run"]) == 0
    assert seen == [["sync", "--dry-run"]]


def test_old_stop_timer_cannot_kill_a_new_process(app, monkeypatch):
    panel = ElearningPanel()
    killed = []
    monkeypatch.setattr(panel, "is_running", lambda: True)
    monkeypatch.setattr(panel.process, "processId", lambda: 222)
    monkeypatch.setattr(panel.process, "kill", lambda: killed.append(True))
    panel.kill_if_running(111)
    assert killed == []
    panel.kill_if_running(222)
    assert killed == [True]


def test_same_name_preservation_has_no_comparison_control_or_pending_prompt(app):
    panel = ElearningPanel()
    assert not hasattr(panel, "compare_existing")
    panel.operation = "sync"
    assert "--compare-existing" not in panel.command()
    panel.apply_payload({"results": [{"course": "fixture", "course_id": "1", "errors": [],
        "files": [{"status": "name_preserved", "name": "notes.pdf", "path": "/fixture/notes.pdf", "bytes": 9,
                   "downloaded_bytes": 0, "note": "保留同名本地文件"}],
        "assignments": {"status": "checked", "assignments": [], "first_run": False, "alerts": []}}]})
    assert panel.file_summary.text() == "同名保留 1"
    assert "待确认" not in panel.output.toPlainText()
    panel.close()


def test_unified_settings_adapter_does_not_reload_or_save_credentials(app, monkeypatch):
    from src.preferences import Preferences
    monkeypatch.setattr(Preferences, "load", lambda *_: pytest.fail("no saved secrets"))
    monkeypatch.setattr(Preferences, "save", lambda *_: pytest.fail("no new credential persistence"))
    values = {**defaults(), "stu_id": "FIXTURE_STUDENT", "uis_psw": "FIXTURE_PASSWORD"}
    window = MainWindow(initial_values=values)
    panel = window.elearning
    assert panel.prepare_credentials()
    assert json.loads(panel.pending_credentials) == {"student_id": "FIXTURE_STUDENT", "password": "FIXTURE_PASSWORD"}
    assert {k: v for k, v in window.collect().items() if k != "elearning"} == {k: v for k, v in values.items() if k != "elearning"}
    panel.pending_credentials = None
    panel.settings_button.click()
    assert window.tabs.currentIndex() == window.settings_index
    window.close()


def test_provider_failure_never_discloses_exception_text(app):
    def provider():
        raise RuntimeError("FIXTURE_PASSWORD_MUST_NOT_APPEAR")
    panel = ElearningPanel(credential_provider=provider)
    panel.launch("check")
    assert "FIXTURE_PASSWORD" not in panel.status.text() + panel.output.toPlainText()
    assert not panel.process.program() and panel.pending_credentials is None
    panel.close()


@pytest.mark.parametrize("student,password", [("", ""), ("FIXTURE_STUDENT", ""), ("", "FIXTURE_PASSWORD")])
def test_missing_login_fields_show_inline_error_without_dialog_or_process(app, student, password):
    panel = ElearningPanel(credential_provider=lambda: dict(student_id=student, password=password))
    panel.refresh_button.click()
    assert "请先在「设置」填写学号和密码" in panel.status.text()
    assert panel.pending_credentials is None and not panel.process.program()
    assert panel.refresh_button.isEnabled() and panel.sync_button.isEnabled()
    panel.close()


def test_manual_submission_stop_repeat_click_and_tab_switch_do_not_resubmit(app, monkeypatch):
    from src.preferences import Preferences
    monkeypatch.setattr(Preferences, "load", lambda *_: pytest.fail("must not load real settings"))
    monkeypatch.setattr(Preferences, "save", lambda *_: pytest.fail("must not save credentials"))
    window = MainWindow(initial_values={**defaults(), "stu_id": "FIXTURE_STUDENT", "uis_psw": "FIXTURE_PASSWORD"})
    panel = window.elearning
    calls = []
    provider = panel.credential_provider
    def current_form():
        calls.append(True)
        return provider()
    panel.credential_provider = current_form
    window.fields["stu_id"].setText("FIXTURE_STUDENT")
    window.fields["uis_psw"].setText("FIXTURE_PASSWORD")
    panel.settings_button.click()
    window.tabs.setCurrentIndex(window.elearning_index)
    assert not calls and not panel.is_running()
    script = "\n".join([
        "import json, signal, sys, time",
        "def stop(*_):",
        "    print(json.dumps(dict(results=[])), flush=True)",
        "    sys.exit(130)",
        "signal.signal(signal.SIGINT, stop)",
        "assert json.load(sys.stdin) == dict(student_id='FIXTURE_STUDENT', password='FIXTURE_PASSWORD')",
        "print('FIXTURE_READY', file=sys.stderr, flush=True)",
        "while True: time.sleep(0.01)",
    ])
    monkeypatch.setattr(panel, "command", lambda: ["-c", script])
    panel.refresh_button.click()
    deadline = time.monotonic() + 10
    while "FIXTURE_READY" not in panel.output.toPlainText() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    try:
        assert "FIXTURE_READY" in panel.output.toPlainText()
        assert calls == [True] and panel.pending_credentials is None
        assert not panel.refresh_button.isEnabled() and not panel.sync_button.isEnabled()
        assert window.tabs.isTabEnabled(window.settings_index)
        assert window.start.isEnabled()
        panel.refresh_button.click()
        panel.sync_button.click()
        panel.launch("sync")
        assert calls == [True] and panel.operation == "check"
        panel.stop_button.click()
        finish(app, panel)
        assert panel.process.exitCode() == 130 and "已停止" in panel.status.text()
        assert calls == [True] and panel.pending_credentials is None
        assert window.tabs.isTabEnabled(window.settings_index)
        panel.settings_button.click()
        assert window.tabs.currentIndex() == window.settings_index
        assert window.fields["uis_psw"].text() == "FIXTURE_PASSWORD"
        assert "FIXTURE_PASSWORD" not in panel.output.toPlainText() + panel.stdout_text
    finally:
        if panel.is_running():
            panel.process.kill()
            panel.process.waitForFinished(3000)
        window.close()


def dashboard_fixture():
    assignments = [
        {"id": "1", "title": "作业 3：基础练习", "due_at": "2020-10-11T15:59:00Z", "submission": "unsubmitted",
         "locked_for_user": False, "unlock_at": "2020-10-05T00:00:00Z", "url": "https://elearning.fudan.edu.cn/courses/115627/assignments/1"},
        {"id": "2", "title": "已提交作业", "due_at": "2026-10-18T15:59:00Z", "submission": "submitted",
         "url": "https://elearning.fudan.edu.cn/courses/115627/assignments/2"},
        {"id": "3", "title": "尚待确认", "due_at": None, "submission": "unknown", "url": "https://untrusted.invalid/"},
    ]
    return {"results": [{"course": "Python程序设计", "course_id": "115627", "errors": [],
        "assignments": {"status": "checked", "assignments": assignments, "first_run": False, "alerts": []},
        "files": [{"status": "downloaded", "name": "a.zip", "bytes": 10},
                  {"status": "duplicate", "name": "b.py", "bytes": 20, "downloaded_bytes": 0},
                  {"status": "failed", "name": "c.pdf", "reason": "长度不符"}]}]}


def test_structured_tasks_filters_dates_status_and_clickable_official_links(app, monkeypatch):
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    panel = ElearningPanel()
    panel.apply_payload(dashboard_fixture())
    assert panel.task_summary.text() == "2 项待办 · 1 项已完成"
    course = panel.task_tree.topLevelItem(0)
    assert course.childCount() == 2
    item = course.child(0)
    assert item.text(1) == "2020-10-11 23:59"
    assert "未提交" in item.text(2) and "已逾期" in item.text(2) and "锁定" not in item.text(2)
    panel.task_tree.itemWidget(item, 3).click()
    assert opened == ["https://elearning.fudan.edu.cn/courses/115627/assignments/1"]
    assert panel.task_tree.itemWidget(course.child(1), 3) is None
    panel.task_filter.setCurrentIndex(2)
    assert panel.task_tree.topLevelItem(0).childCount() == 1
    assert "已提交" in panel.task_tree.topLevelItem(0).child(0).text(2)
    panel.task_filter.setCurrentIndex(1)
    assert panel.task_tree.topLevelItem(0).childCount() == 3
    assert panel.file_summary.text() == "新增 1 · 复用 1 · 失败 1"
    assert not panel.details_toggle.isChecked()
    panel.close()


@pytest.mark.parametrize("url", ["https://evil.invalid/courses/1/assignments/1", "javascript:alert(1)",
    "file:///tmp/file", "https://elearning.fudan.edu.cn/login", "https://elearning.fudan.edu.cn:444/courses/1/assignments/1"])
def test_official_link_guard_never_opens_unapproved_targets(app, monkeypatch, url):
    from PySide6.QtGui import QDesktopServices
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda _: pytest.fail("unapproved link"))
    panel = ElearningPanel()
    panel.open_assignment(url)
    panel.close()


def test_failed_task_read_is_not_displayed_as_no_assignments(app):
    panel = ElearningPanel()
    panel.apply_payload({"results": [{"course": "fixture", "course_id": "1", "errors": ["读取失败"], "files": []}]})
    assert "读取失败" in panel.task_summary.text()
    assert "未能读取" in panel.task_tree.topLevelItem(0).child(0).text(0)
    panel.close()


@pytest.mark.parametrize("filter_index,visible_count", [(0, 2), (1, 3), (2, 1)])
def test_locked_tasks_hidden_from_each_filter_and_counts_without_mutating_results(app, filter_index, visible_count):
    import copy
    payload = dashboard_fixture()
    items = payload["results"][0]["assignments"]["assignments"]
    items.extend([{**items[0], "id": "4", "title": "锁定的待办", "locked_for_user": True},
                  {**items[1], "id": "5", "title": "锁定的已提交历史作业", "locked_for_user": True}])
    snapshot = copy.deepcopy(payload)
    panel = ElearningPanel()
    panel.task_filter.setCurrentIndex(filter_index)
    panel.apply_payload(payload)
    assert panel.task_summary.text() == "2 项待办 · 1 项已完成"
    course = panel.task_tree.topLevelItem(0)
    assert course.text(0) == "Python程序设计  ·  待办 2 / 已完成 1"
    assert course.childCount() == visible_count
    assert all("锁定的" not in course.child(i).text(0) for i in range(course.childCount()))
    assert payload == snapshot and len(panel.results[0]["assignments"]["assignments"]) == 5
    panel.close()


@pytest.mark.parametrize("filter_index", [0, 1, 2])
def test_courses_with_only_locked_or_no_assignments_have_no_empty_groups(app, filter_index):
    import copy
    payload = dashboard_fixture()
    for item in payload["results"][0]["assignments"]["assignments"]:
        item["locked_for_user"] = True
    empty = copy.deepcopy(payload["results"][0])
    empty["course"] = "没有作业的课程"
    empty["assignments"]["assignments"] = []
    payload["results"].append(empty)
    panel = ElearningPanel()
    panel.task_filter.setCurrentIndex(filter_index)
    panel.apply_payload(payload)
    assert panel.task_tree.topLevelItemCount() == 0
    assert panel.task_summary.text() == "0 项待办 · 0 项已完成"
    panel.close()


def test_unlock_refresh_reappears_and_keeps_baseline_and_deadline_alerts(app, tmp_path):
    from src.elearning_helper.demo import DemoClient, demo_config
    from src.elearning_helper.state import Store
    from src.elearning_helper.sync import check_assignments
    config = demo_config(tmp_path)
    course = config.courses[0]
    client = DemoClient()
    client.assignment_rows = [{"id": 1, "name": "暂时锁定的作业", "locked_for_user": True,
                               "due_at": "2026-10-11T15:59:00Z", "submission": {"workflow_state": "unsubmitted"}}]
    store = Store(config.state_dir / "index.sqlite3")
    panel = ElearningPanel()
    def refresh():
        result = check_assignments(client, store, config, course, dry_run=False)
        panel.apply_payload({"results": [{"course": course.name, "course_id": course.id,
                                           "errors": [], "files": [], "assignments": result}]})
        return result
    try:
        baseline = refresh()
        assert baseline["assignments"][0]["locked_for_user"]
        assert [alert["kind"] for alert in baseline["alerts"]] == ["baseline"]
        assert "1" in store.assignments(course.id) and panel.task_tree.topLevelItemCount() == 0
        client.assignment_rows[0]["due_at"] = "2026-10-12T15:59:00Z"
        changed = refresh()
        assert [alert["kind"] for alert in changed["alerts"]] == ["deadline_changed"]
        assert panel.task_tree.topLevelItemCount() == 0
        client.assignment_rows[0]["locked_for_user"] = False
        unlocked = refresh()
        assert unlocked["alerts"] == []
        assert panel.task_summary.text() == "1 项待办 · 0 项已完成"
        assert panel.task_tree.topLevelItemCount() == 1
        assert panel.task_tree.topLevelItem(0).child(0).text(1) == "2026-10-12 23:59"
        assert refresh()["alerts"] == [] and len(store.alerts()) == 2
    finally:
        panel.close()
        store.close()
