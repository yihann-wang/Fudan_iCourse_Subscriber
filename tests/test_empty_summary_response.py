import copy
from types import SimpleNamespace

import pytest

from src.config import LLMProvider
from src.summarizer import Summarizer
from src.summary_errors import EmptySummaryResponseError, TruncatedSummaryError


@pytest.fixture
def service(monkeypatch):
    def make(transport, responses):
        monkeypatch.setenv("LLM_TRANSPORT", transport)
        provider = LLMProvider(name="test", api_key="not-real", base_url="http://localhost:1/v1",
                               models=["test"], api_style="anthropic" if transport == "anthropic" else "openai")
        summarizer = Summarizer(providers=[provider], timeout_seconds=1)
        calls, waits = [], []
        replies = iter(responses)
        monkeypatch.setattr("src.summarizer.time.sleep", waits.append)

        def request(**kwargs):
            calls.append(copy.deepcopy(kwargs))
            reply = next(replies)
            if transport == "sdk":
                return SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason=c["finish_reason"],
                    message=SimpleNamespace(**c["message"]) if c.get("message") else None,
                ) for c in reply.get("choices", [])])
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: reply)

        if transport == "sdk":
            summarizer.providers[0]["client"] = SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=request)))
        else:
            monkeypatch.setattr("src.summarizer.requests.post", lambda url, **kwargs: request(**kwargs))
        return summarizer, calls, waits
    return make


def answer(transport, content, *, reason=None):
    if transport == "anthropic":
        return {"stop_reason": reason or "end_turn", "content": [
            {"type": "thinking", "thinking": "internal reasoning is not an answer"},
            {"type": "text", "text": content},
        ]}
    return {"choices": [{"finish_reason": reason or "stop", "message": {
        "content": content, "reasoning_content": "internal reasoning is not an answer",
    }}]}


@pytest.mark.parametrize("transport", ["sdk", "http", "anthropic"])
@pytest.mark.parametrize("content", [None, "", " \n\t"])
def test_empty_response_retries_identical_request_then_accepts_answer(transport, content, service):
    s, calls, waits = service(transport, [answer(transport, content), answer(transport, '{"text":"正文"}')])
    result = s._call_with_retry(s.providers[0], "test", "课", "完整课程原文", system_prompt="整课写作")
    assert result == '{"text":"正文"}'
    assert len(calls) == 2 and calls[0] == calls[1]
    assert waits == [5]


@pytest.mark.parametrize("transport", ["sdk", "http", "anthropic"])
def test_repeated_empty_response_stops_after_three_attempts(transport, service):
    s, calls, waits = service(transport, [answer(transport, "")] * 3)
    with pytest.raises(EmptySummaryResponseError, match="空响应"):
        s._call_with_retry(s.providers[0], "test", "课", "完整课程原文")
    assert len(calls) == 3 and all(c == calls[0] for c in calls)
    assert waits == [5, 15]


@pytest.mark.parametrize("transport,empty", [
    ("sdk", {"choices": []}), ("http", {"choices": []}),
    ("sdk", {"choices": [{"finish_reason": "stop", "message": None}]}),
    ("http", {"choices": [{"finish_reason": "stop", "message": None}]}),
    ("anthropic", {"stop_reason": "end_turn", "content": None}),
])
def test_missing_answer_is_also_retryable(transport, empty, service):
    s, calls, waits = service(transport, [empty, answer(transport, "正文")])
    assert s._call_with_retry(s.providers[0], "test", "课", "原文") == "正文"
    assert len(calls) == 2 and waits == [5]


@pytest.mark.parametrize("transport", ["sdk", "http", "anthropic"])
@pytest.mark.parametrize("failure", ["truncation", "refusal"])
def test_empty_body_does_not_hide_abnormal_stop_reason(transport, failure, service):
    if failure == "truncation":
        reason = "max_tokens" if transport == "anthropic" else "length"
        error = TruncatedSummaryError
    else:
        reason = "refusal" if transport == "anthropic" else "content_filter"
        error = RuntimeError
    s, calls, waits = service(transport, [answer(transport, "", reason=reason)])
    with pytest.raises(error) as exc:
        s._call_with_retry(s.providers[0], "test", "课", "原文")
    assert not isinstance(exc.value, EmptySummaryResponseError)
    assert len(calls) == 1 and waits == []
