"""Future/locked assignments are visible metadata, independent of content access."""

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest

from src.elearning_helper.__main__ import display
from src.elearning_helper.api import CanvasClient, ConnectionFailure, pagination_next
from src.elearning_helper.assignments import availability
from src.elearning_helper.demo import DemoClient, DemoResponse, demo_config
from src.elearning_helper.state import Store
from src.elearning_helper.sync import check_assignments, normalize_assignment

BASE = "https://elearning.fudan.edu.cn"


def future_assignment(aid=136327):
    return {"id": aid, "name": "作业3: 列表操作与鸡兔同笼", "published": True,
            "locked_for_user": True, "due_at": "2026-10-11T15:59:00Z",
            "lock_at": "2026-12-25T15:59:00Z", "unlock_at": "2026-09-01T00:00:00Z",
            "lock_info": {"context_module": {"name": "第5周", "unlock_at": "2026-10-04T16:00:00Z"}},
            "submission_types": ["none"]}


@pytest.fixture
def setup(tmp_path):
    config = demo_config(tmp_path)
    store = Store(config.state_dir / "index.sqlite3")
    try:
        yield config, config.courses[0], store
    finally:
        store.close()


def test_locked_metadata_is_retained_and_separately_labelled_in_output(setup, capsys):
    config, course, store = setup
    client = DemoClient()
    client.assignment_rows = [future_assignment()]
    client.open = Mock(side_effect=AssertionError("must not request locked content"))
    result = check_assignments(client, store, config, course, dry_run=False)
    assert [item["id"] for item in result["assignments"]] == ["136327"]
    assert result["first_run"] and result["alerts"][0]["kind"] == "baseline"
    assert "136327" in store.assignments(course.id)
    assert result["counts"] == {"received": 1, "listed": 1, "locked": 1, "unpublished": 0, "pages": None}
    display([{"course": course.name, "course_id": course.id, "errors": [], "files": [], "assignments": result}], "Asia/Shanghai")
    output = capsys.readouterr().out
    assert "作业清单（1 项）" in output
    assert "作业3" in output and "2026-10-11 23:59" in output
    assert "无需在线提交" in output
    assert any(label in output for label in ("尚未开放", "已关闭", "暂不可用"))
    assert result["assignments"][0]["lock_at"] == "2026-12-25T15:59:00+00:00"
    client.open.assert_not_called()


def test_locked_new_assignment_and_deadline_change_are_not_suppressed(setup):
    config, course, store = setup
    client = DemoClient()
    check_assignments(client, store, config, course, dry_run=False)
    client.assignment_rows.append(future_assignment())
    new = check_assignments(client, store, config, course, dry_run=False)
    assert [(x["kind"], x["id"]) for x in new["alerts"]] == [("new_assignment", "136327")]
    client.assignment_rows[-1]["due_at"] = "2026-10-12T15:59:00Z"
    changed = check_assignments(client, store, config, course, dry_run=False)
    assert changed["alerts"][0]["kind"] == "deadline_changed"
    assert changed["alerts"][0]["locked_for_user"]
    client.assignment_rows[-1]["locked_for_user"] = False
    opened = check_assignments(client, store, config, course, dry_run=False)
    assert not opened["alerts"]  # same assignment becoming accessible is not a new assignment
    assert len(opened["assignments"]) == 2


def test_normal_output_separates_locked_history_and_preserves_alerts(setup, capsys):
    config, course, store = setup
    client = DemoClient()
    check_assignments(client, store, config, course, dry_run=False)
    client.assignment_rows.extend([
        {**future_assignment(21), "name": "隐藏的锁定待办"},
        {**future_assignment(22), "name": "隐藏的锁定历史", "submission": {"workflow_state": "submitted"}},
    ])
    checked = check_assignments(client, store, config, course, dry_run=False)
    snapshot = json.dumps(checked, ensure_ascii=False)
    result = {"course": course.name, "course_id": course.id, "errors": [], "files": [], "assignments": checked}
    display([result], "Asia/Shanghai")
    output = capsys.readouterr().out
    assert "作业清单（3 项）" in output and "隐藏的锁定待办" in output and "隐藏的锁定历史" in output
    assert "[新作业]" in output and "当前可访问（1 项）" in output
    assert json.dumps(checked, ensure_ascii=False) == snapshot
    assert checked["counts"]["listed"] == 3 and checked["counts"]["locked"] == 2
    assert len(checked["alerts"]) == 2 and len(store.assignments(course.id)) == 3
    for item in client.assignment_rows:
        item["locked_for_user"] = False
    result["assignments"] = check_assignments(client, store, config, course, dry_run=False)
    assert result["assignments"]["alerts"] == []
    display([result], "Asia/Shanghai")
    output = capsys.readouterr().out
    assert "作业清单（3 项）" in output and "隐藏的锁定待办" in output and "隐藏的锁定历史" in output
    assert "当前可访问（3 项）" in output


@pytest.mark.parametrize("changes,expected", [
    ({"locked_for_user": False, "lock_at": "2020-01-01T00:00:00Z"}, "open"),
    ({"due_at": "2020-01-01T00:00:00Z"}, "locked"),
    ({"unlock_at": "2026-10-19T00:00:00+08:00"}, "upcoming"),
    ({"unlock_at": "2026-10-14T00:00:00Z"}, "locked"),
    ({"lock_at": "2026-10-14T00:00:00Z"}, "closed"),
    ({"lock_at": "invalid", "unlock_at": "2026-10-19T00:00:00"}, "locked"),
    ({"submission": "graded"}, "locked"),
])
def test_availability_uses_explicit_lock_and_aware_dates_never_due_or_submission(changes, expected):
    now = datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert availability({"locked_for_user": True, **changes}, now) == expected


def test_normalize_retains_effective_open_close_dates_without_replacing_due():
    row = future_assignment()
    row["lock_info"]["lock_at"] = "2026-10-12T00:00:00+08:00"
    item = normalize_assignment(row, "115627", BASE)
    assert item["unlock_at"] == "2026-10-04T16:00:00+00:00"
    assert item["lock_at"] == "2026-10-11T16:00:00+00:00"
    assert item["due_at"] == "2026-10-11T15:59:00+00:00"
    row.update(lock_at="invalid", lock_info={"lock_at": "2026-10-12T00:00:00"})
    item = normalize_assignment(row, "115627", BASE)
    assert item["lock_at"] is None and item["due_at"] == "2026-10-11T15:59:00+00:00"


@pytest.mark.parametrize("submission,expected", [(None, "unknown"), ({"workflow_state": "unsubmitted"}, "unsubmitted"),
                                                   ({"workflow_state": "submitted"}, "submitted")])
def test_lock_and_dashboard_completion_never_determine_submission(submission, expected):
    row = future_assignment()
    row.update(submission_types=["online_upload"], submission=submission,
               planner_override={"marked_complete": False}, has_submitted_submissions=True)
    normalized = normalize_assignment(row, "115627", BASE)
    assert normalized["submission"] == expected


def test_no_online_submission_does_not_override_grade_or_excusal():
    row = future_assignment()
    row["submission"] = {"workflow_state": "graded"}
    assert normalize_assignment(row, "115627", BASE)["submission"] == "graded"
    row["submission"] = {"excused": True}
    assert normalize_assignment(row, "115627", BASE)["submission"] == "excused"


def test_invalid_or_missing_optional_unlock_date_does_not_hide_due(setup):
    config, course, store = setup
    client = DemoClient()
    row = future_assignment()
    row.update(unlock_at="invalid", lock_info={"context_module": "{}"}, submission_types=[])
    client.assignment_rows = [row, {**future_assignment(99), "published": False}]
    result = check_assignments(client, store, config, course, dry_run=True)
    assert len(result["assignments"]) == 1
    assert result["assignments"][0]["unlock_at"] is None
    assert result["assignments"][0]["due_at"].startswith("2026-10-11")
    assert result["counts"]["unpublished"] == 1
    assert not store.course(course.id)


def test_two_completed_then_ten_future_items_across_pages_are_all_listed(setup):
    config, course, store = setup
    client = CanvasClient(BASE, "TEST_ONLY_FIXTURE")
    past = [{"id": 136325+i, "name": f"past {i}", "submission": {"workflow_state": "submitted"}} for i in range(2)]
    future = [future_assignment(136327+i) for i in range(10)]
    first = DemoResponse(json.dumps(past).encode(), content_type="application/json")
    # Lowercase header, parameters before rel, a quoted comma, and opaque next URL.
    next_url = BASE + f"/api/v1/courses/{course.id}/assignments?opaque=fixture%2Bnext"
    first.headers["link"] = f'<{next_url}>; title="page, next"; type="application/json"; rel="next alternate"'
    second = DemoResponse(json.dumps(future).encode(), content_type="application/json")
    client.open = Mock(side_effect=[first, second])
    result = check_assignments(client, store, config, course, dry_run=True)
    assert len(result["assignments"]) == 12
    assert result["counts"] == {"received": 12, "listed": 12, "locked": 10, "unpublished": 0, "pages": 2}
    assert client.open.call_args_list[1].args[0] == next_url
    query = parse_qs(urlsplit(client.open.call_args_list[0].args[0]).query)
    assert query == {"per_page": ["100"], "include[]": ["submission"], "override_assignment_dates": ["true"]}
    assert store.course(course.id) is None


@pytest.mark.parametrize("header", ["invalid", '<https://example.invalid/page>; title="no relation"',
                                     '<https://a.invalid>; rel="next", <https://b.invalid>; rel="next"'])
def test_unusable_pagination_cannot_initialize_baseline(setup, header):
    config, course, store = setup
    client = CanvasClient(BASE, "TEST_ONLY_FIXTURE")
    response = DemoResponse(json.dumps([future_assignment()]).encode(), content_type="application/json")
    response.headers["Link"] = header
    client.open = Mock(return_value=response)
    with pytest.raises(ConnectionFailure):
        check_assignments(client, store, config, course, dry_run=False)
    assert not store.course(course.id) and not store.assignments(course.id)
    assert not client.page_counts


def test_multiple_link_headers_with_token_relation_are_followed():
    response = DemoResponse(b"[]")
    response.headers["LiNk"] = f'<{BASE}/current>; rel="current"'
    response.headers["LINK"] = f'<{BASE}/next>; type="application/json"; rel=next'
    assert pagination_next(response.headers, BASE + "/current") == BASE + "/next"
