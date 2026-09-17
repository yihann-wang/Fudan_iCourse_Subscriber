import json
import queue
import threading
from types import SimpleNamespace

import pytest

from src.artifacts import internal_path
from src.config import LLMProvider
from src.summarizer import Summarizer, TruncatedSummaryError

SOURCE = "课程开头。\n" + "课堂解释与例子。" * 4000 + "\n结尾补充前面的概念。"
NOTES = "### 核心概念\n\n完整连贯的第一段。\n\n### 案例\n\n联系前后原文的第二段。"


@pytest.fixture
def factory(monkeypatch):
    monkeypatch.setenv("LLM_TRANSPORT", "sdk")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "65536")
    monkeypatch.setattr("src.summarizer.time.sleep", lambda *_: None)

    def make(replies=None):
        provider = LLMProvider(name="test", api_key="not-real", base_url="https://api.deepseek.com", models=["test"])
        summarizer = Summarizer(providers=[provider], timeout_seconds=1)
        calls = []
        responses = iter(replies) if replies is not None else None

        def create(**kwargs):
            calls.append(kwargs)
            reply = next(responses) if responses is not None else ("stop", NOTES)
            if isinstance(reply, Exception):
                raise reply
            reason, text = reply
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason,
                                   message=SimpleNamespace(content=text))])
        summarizer.providers[0]["client"] = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        return summarizer, calls
    return make


def test_full_transcript_one_request_direct_markdown(factory, monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_INPUT_CHAR_LIMIT", "1000")
    s, calls = factory()
    cache = tmp_path / "progress.json"
    result = s.summarize("课程", SOURCE, checkpoint_path=cache)
    assert result.text == NOTES and result.model == "test/test"
    assert len(calls) == 1
    assert calls[0]["messages"][1]["content"] == f"以下是课程《课程》的录音文本，请根据完整原文一次生成整篇笔记：\n\n{SOURCE}"
    assert "[TASK:" not in calls[0]["messages"][0]["content"]
    assert "response_format" not in calls[0] and calls[0]["max_tokens"] == 65536
    assert "not-real" not in cache.read_text()
    restarted, later_calls = factory()
    assert restarted.summarize("课程", SOURCE, checkpoint_path=cache) == result
    assert later_calls == []


def test_truncation_is_not_saved_or_split_into_extra_requests(factory, tmp_path):
    s, calls = factory([("length", "截断的正文")])
    cache = tmp_path / "progress.json"
    with pytest.raises(TruncatedSummaryError, match="输出上限"):
        s.summarize("课程", SOURCE, checkpoint_path=cache)
    assert len(calls) == 1 and calls[0]["messages"][1]["content"].endswith(SOURCE)
    assert json.loads(cache.read_text())["result"] is None


def test_empty_answer_retry_preserves_full_input_without_extra_stages(factory, tmp_path):
    s, calls = factory([("stop", None), ("stop", NOTES)])
    result = s.summarize("课程", SOURCE, checkpoint_path=tmp_path / "progress.json")
    assert result.text == NOTES and len(calls) == 2 and calls[0] == calls[1]


def test_repeated_empty_answer_keeps_incomplete_cache(factory, tmp_path):
    s, calls = factory([("stop", None)] * 3)
    cache = tmp_path / "progress.json"
    with pytest.raises(RuntimeError, match="空响应"):
        s.summarize("课程", SOURCE, checkpoint_path=cache)
    assert len(calls) == 3 and json.loads(cache.read_text())["result"] is None
    restarted, later_calls = factory()
    assert restarted.summarize("课程", SOURCE, checkpoint_path=cache).text == NOTES
    assert len(later_calls) == 1


@pytest.mark.parametrize("change", ["content", "title", "model", "budget", "corrupt", "overwrite"])
def test_changed_settings_do_not_reuse_wrong_result(change, factory, tmp_path):
    cache = tmp_path / "progress.json"
    s, _ = factory()
    s.summarize("课程", SOURCE, checkpoint_path=cache)
    original = cache.read_bytes()
    restarted, calls = factory()
    if change == "model":
        restarted.providers[0]["models"] = ["different"]
    if change == "budget":
        restarted.max_output_tokens += 1024
    if change == "corrupt":
        cache.write_text(cache.read_text().replace("完整连贯", "篡改正文"))
        original = cache.read_bytes()
    restarted.summarize("新课" if change == "title" else "课程",
                        SOURCE + "新增内容" if change == "content" else SOURCE,
                        checkpoint_path=cache, overwrite=change == "overwrite")
    assert len(calls) == 1
    assert any(p.read_bytes() == original for p in (tmp_path / "history").rglob("*.json"))


def test_old_multistage_checkpoint_is_archived_not_resumed(factory, tmp_path):
    cache = tmp_path / "progress.json"
    old = '{"schema":2,"workflow":"whole-lecture-v1","state":{"repairs":2}}'
    cache.write_text(old)
    s, calls = factory()
    assert s.summarize("课程", SOURCE, checkpoint_path=cache).text == NOTES
    assert len(calls) == 1 and json.loads(cache.read_text())["workflow"] == "single-pass-v1"
    assert any(p.read_text() == old for p in (tmp_path / "history").rglob("*.json"))


def test_preflight_disk_failure_never_calls_api(factory, tmp_path, monkeypatch):
    s, calls = factory()
    def fail(*a, **k):
        raise OSError("disk unavailable")
    monkeypatch.setattr("src.summarizer.atomic_write_json", fail)
    with pytest.raises(OSError):
        s.summarize("课程", SOURCE, checkpoint_path=tmp_path / "progress.json")
    assert calls == []


def test_empty_transcript_never_calls_api(factory):
    s, calls = factory()
    with pytest.raises(ValueError, match="转录为空"):
        s.summarize("课程", " \n ")
    assert calls == []


@pytest.mark.parametrize("failed_write", ["note", "metadata"])
def test_commit_retry_reuses_generated_note_without_api(failed_write, factory, tmp_path, monkeypatch):
    from src import pipeline
    from src.pipeline_state import valid_artifact
    transcript, notes = tmp_path / "课_123456.txt", tmp_path / "课_123456.md"
    transcript.write_text(SOURCE)
    notes.write_text("旧笔记")
    task = dict(course_id="12345", sub_id="123456", course_title="课程", sub_title="课",
                target_transcript_path=transcript, target_summary_path=notes)
    def run(s):
        tasks = queue.Queue()
        tasks.put(task)
        tasks.put(None)
        counters = pipeline._Counters()
        pipeline._summarize_stage(tasks, s, 0, counters, set(), threading.Lock())
        assert tasks.unfinished_tasks == 0
        return counters
    name = "atomic_write_text" if failed_write == "note" else "artifact_metadata"
    original_writer = getattr(pipeline, name)
    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(pipeline, name, fail)
    s, calls = factory()
    assert run(s).failed == 1 and len(calls) == 1
    cache = internal_path(notes.with_suffix(".summary-progress.json"))
    assert json.loads(cache.read_text())["result"]["text"] == NOTES
    assert any(p.read_text() == "旧笔记" for p in (tmp_path / ".icourse/history").rglob("*.md"))
    monkeypatch.setattr(pipeline, name, original_writer)
    restarted, calls = factory()
    counters = run(restarted)
    assert counters.failed == 0 and counters.summarized == 1 and calls == []
    assert notes.read_text() == "### 课\n\n" + NOTES + "\n"
    assert valid_artifact(notes) and not cache.exists()


def test_one_visible_note_after_legacy_export_migration(factory, tmp_path):
    from src.pipeline import _Counters, _summarize_stage, _find_file_by_sub_id, _scan_summarized_sub_ids
    from src.pipeline_state import PipelineState, artifact_metadata, valid_artifact
    notes = tmp_path / "笔记" / "课_123456.md"
    transcript = tmp_path / "原始txt" / "课_123456.txt"
    transcript.parent.mkdir()
    transcript.write_text(SOURCE)
    notes.parent.mkdir()
    notes.write_text("旧正式笔记")
    draft = notes.parent / "待核对" / notes.name
    draft.parent.mkdir()
    for path, kind in ((draft, "summary_draft"), (draft.with_suffix(".核对意见.md"), "review_report")):
        path.write_text("旧辅助输出：" + kind)
        artifact_metadata(path, source=transcript, kind=kind, status="needs_review",
                          course_id="12345", sub_id="123456")
    task = dict(course_id="12345", sub_id="123456", course_title="课程", sub_title="课",
                target_transcript_path=transcript, target_summary_path=notes)
    state = PipelineState(tmp_path / "state.sqlite")
    try:
        tasks = state.queue("sm")
        tasks.put(task)
        tasks.put(None)
        counters = _Counters()
        s, calls = factory()
        _summarize_stage(tasks, s, 0, counters, set(), threading.Lock())
        assert counters.summarized == 1 and counters.failed == 0 and len(calls) == 1
        row = state.connection.execute("SELECT status FROM pipeline_jobs WHERE sub_id='123456'").fetchone()
        assert row["status"] == "done"
    finally:
        state.close()
    visible = [p for p in tmp_path.rglob("*.md") if ".icourse" not in p.relative_to(tmp_path).parts]
    assert visible == [notes] and not draft.parent.exists()
    assert notes.read_text() == "### 课\n\n" + NOTES + "\n"
    record = json.loads(internal_path(notes.with_suffix(".md.icourse.json")).read_text())
    assert record["status"] == "complete" and record["generation"] == "single_pass"
    assert "review" not in record and valid_artifact(notes)
    archived = [p.read_text() for p in (notes.parent / ".icourse/history").rglob("*.md")]
    assert set(archived) == {"旧正式笔记", "旧辅助输出：summary_draft", "旧辅助输出：review_report"}
    assert _find_file_by_sub_id([tmp_path], "123456", ".md") == notes
    notes.unlink()
    assert _find_file_by_sub_id([tmp_path], "123456", ".md") is None
    assert _scan_summarized_sub_ids([tmp_path]) == set()


def test_tiny_budget_rejected_before_billing_long_lecture(factory, tmp_path):
    s, calls = factory()
    s.max_output_tokens = 1024
    with pytest.raises(ValueError, match='本次未调用笔记 API'):
        s.summarize('课程', SOURCE, checkpoint_path=tmp_path/'progress.json')
    assert calls == []
