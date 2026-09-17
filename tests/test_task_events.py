import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from src import task_events as events
from src.task_view_model import TaskViewModel, percent


def feed(model, kind, **data):
    event = dict(event="icourse", version=1, run_id=model.run_id, seq=model.seq + 1,
                 kind=kind, time="12:00:00", **data)
    assert model.apply(event)
    return event


def task(model, course="101", sub="123456", **stages):
    feed(model, "task", course_id=course, sub_id=sub, course_title=f"课程 {course}", title="第一课",
         stages=stages or dict(dl="cached", tr="cached", sm="queued"))


def test_courses_same_sub_id_and_partial_failure():
    model = TaskViewModel("run")
    task(model)
    task(model, "102")
    feed(model, "planned", total=2)
    for course, status in (("101", "done"), ("102", "failed")):
        feed(model, "stage", course_id=course, sub_id="123456", stage="sm", status="running")
        feed(model, "stage", course_id=course, sub_id="123456", stage="sm", status=status, message="结果")
    feed(model, "run_finished", status="failed")
    model.finish(1)
    assert model.counts()["success"] == model.counts()["failed"] == 1
    assert model.counts("101")["success"] == 1 and model.counts("102")["failed"] == 1
    assert "部分课次" in model.heading


def test_real_progress_no_fake_llm_percent_and_no_heartbeat_spam():
    model = TaskViewModel("run")
    task(model)
    feed(model, "stage", course_id="101", sub_id="123456", stage="sm", status="running")
    for tick in range(600):
        feed(model, "heartbeat", pid=123)
        feed(model, "progress", course_id="101", sub_id="123456", stage="sm", message="等待返回",
             metrics=dict(phase="request", model="test", attempt=1, elapsed=tick))
    assert len(model.records) == 2
    stage = model.tasks[("101", "123456")].stages["sm"]
    assert percent(stage) is None
    stage.metrics = dict(unit="bytes", completed=512, total=1024)
    assert percent(stage) == 50
    stage.metrics["total"] = 0
    assert percent(stage) is None


@pytest.mark.parametrize("cancelled,crashed,outcome", [(True, False, "cancelled"), (False, True, "interrupted"), (False, False, "interrupted")])
def test_unfinished_jobs_never_appear_successful(cancelled, crashed, outcome):
    model = TaskViewModel("run")
    task(model, "101", dl="cached", tr="cached", sm="cached")
    task(model, "102")
    model.finish(0, cancelled=cancelled, crashed=crashed)
    assert model.counts()["success"] == 1 and model.counts()[outcome] == 1
    late = dict(event="icourse", version=1, run_id="run", seq=99, kind="run_finished", status="done")
    assert not model.apply(late)
    model.finish(0)
    assert model.final == outcome


def test_ordered_protocol_rejects_previous_run_and_duplicates():
    model = TaskViewModel("new")
    event = feed(model, "heartbeat", pid=1)
    assert not model.apply(event)
    assert not model.apply({**event, "seq": 2, "run_id": "old"})
    assert model.seq == 1


def test_threaded_event_identity_and_redaction(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setenv("ICOURSE_EVENTS", "json")
    monkeypatch.setattr(sys, "stdout", stream)
    secret = uuid4().hex
    monkeypatch.setenv("LLM_API_KEY_1", secret)
    events.begin_run(uuid4().hex)

    def worker(course):
        value = dict(course_id=course, sub_id="same", sub_title="课")
        events.bind_task(value, "sm")
        for _ in range(30):
            events.progress(f"error {secret} https://example.invalid/signed?token=value", phase="request")

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(worker, ["101", "102", "103"]))
    output = stream.getvalue()
    records = [json.loads(line) for line in output.splitlines()]
    assert [e["seq"] for e in records] == list(range(1, 92))
    assert secret not in output and "example.invalid" not in output
    assert {e["course_id"] for e in records[1:]} == {"101", "102", "103"}


def test_engine_stdout_is_only_protocol_and_stderr_contains_diagnostics(tmp_path):
    env = dict(os.environ, ICOURSE_EVENTS="json", ICOURSE_RUN_ID=uuid4().hex,
               ICOURSE_STATE_DIR=str(tmp_path), ICOURSE_KEEP_AWAKE="0")
    result = subprocess.run([sys.executable, "-m", "src.engine", "doctor"], env=env,
                            capture_output=True, text=True, timeout=30)
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert records and all(e["run_id"] == env["ICOURSE_RUN_ID"] for e in records)
    assert records[0]["kind"] == "run_started" and records[-1]["kind"] == "run_finished"
    assert '"system"' in result.stderr


def test_token_budgets_are_visible_but_actual_tokens_stay_secret(monkeypatch):
    secret = uuid4().hex
    numeric_password = '88776655'
    env = dict(LLM_MAX_OUTPUT_TOKENS='65536', TOKEN_LIMIT='8192',
               ANTHROPIC_AUTH_TOKEN=secret, LLM_API_KEY_1=uuid4().hex, UISPsw=numeric_password)
    monkeypatch.setattr(events, '_secrets', ())
    events.set_secrets(events.environment_secrets(env))
    text = events.redact(f'输出上限 65536 tokens / 8192; {secret}; {numeric_password}')
    assert '65536' in text and '8192' in text
    assert secret not in text and numeric_password not in text


def test_new_audio_chunks_and_empty_results_are_logged_without_tick_spam():
    model = TaskViewModel('chunks')
    task(model, dl='cached', tr='queued', sm='waiting')
    feed(model, 'stage', course_id='101', sub_id='123456', stage='tr', status='running')
    before = len(model.records)
    for chunk in (1, 2):
        for _ in range(10):
            feed(model, 'progress', course_id='101', sub_id='123456', stage='tr',
                 message=f'音频块 {chunk}', metrics=dict(phase='transcribing', chunk=chunk))
    assert len(model.records) == before + 2
    for _ in range(2):
        feed(model, 'progress', course_id='101', sub_id='123456', stage='tr',
             message='本块为空', metrics=dict(phase='transcribing', chunk=2, notice=True))
    assert len(model.records) == before + 3
