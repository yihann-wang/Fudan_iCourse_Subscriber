import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import LLMProvider
from src.preferences import Preferences, SECRET_FIELDS, defaults, runtime_environment
from src.summarizer import Summarizer, TruncatedSummaryError


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, key, value):
        self.values[service, key] = value

    def get_password(self, service, key):
        return self.values.get((service, key))


def test_secrets_never_written_to_preferences(tmp_path):
    path = tmp_path / "settings.json"
    store = Preferences(path, MemoryKeyring())
    # Synthetic values exist only during this test; no real account is contacted.
    fake_password = uuid4().hex
    fake_api_key = uuid4().hex
    values = defaults()
    values.update(uis_psw=fake_password, llm_api_key_1=fake_api_key)
    store.save(values)
    text = path.read_text()
    assert fake_password not in text and fake_api_key not in text
    assert all(field not in json.loads(text) for field in SECRET_FIELDS)
    assert store.load()["uis_psw"] == values["uis_psw"]
    assert path.stat().st_mode & 0o777 == 0o600


def test_keychain_failure_has_no_plaintext_fallback(tmp_path):
    class FailingKeyring(MemoryKeyring):
        def set_password(self, *_):
            raise RuntimeError("locked")
    path = tmp_path / "prefs.json"
    with pytest.raises(RuntimeError, match="locked"):
        Preferences(path, FailingKeyring()).save(defaults())
    assert not path.exists()


def test_legacy_windows_paths_and_false_string_migrate(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"out_dir": "E:/course", "summary_dir": "D:\\notes", "overwrite": "false"}))
    prefs = Preferences(tmp_path / "new.json", MemoryKeyring())
    values = prefs.load(legacy)
    assert values["out_dir"] == defaults()["out_dir"]
    assert not values["overwrite"]
    assert legacy.exists()


def test_gui_environment_is_isolated_and_explicit():
    base = {"LLM_API_KEY_2": "unrelated", "WHISPER_DEVICE": "cuda", "PATH": "/bin"}
    env = runtime_environment(defaults(), base)
    assert "LLM_API_KEY_2" not in env and "WHISPER_DEVICE" not in env
    assert env["ASR_BACKEND"] == "auto" and env["PATH"] == "/bin"
    assert base["WHISPER_DEVICE"] == "cuda"


@pytest.fixture
def summarizer():
    provider = LLMProvider(name="test", api_key="not-real", base_url="http://localhost:1/v1", models=["test"])
    return Summarizer(providers=[provider], timeout_seconds=1)


def test_sdk_output_truncation_is_failure(summarizer):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="partial"))])
    provider = {"client": SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: response)))}
    with pytest.raises(TruncatedSummaryError):
        summarizer._call_openai_sdk(provider, "test", "course", "text")


def test_sdk_failure_does_not_duplicate_request_via_http(summarizer, monkeypatch):
    def fail(*_):
        raise RuntimeError("timeout")
    monkeypatch.setattr(summarizer, "_call_openai_sdk", fail)
    monkeypatch.setattr(summarizer, "_call_openai_http", lambda *_: pytest.fail("Unexpected duplicate HTTP request"))
    with pytest.raises(RuntimeError, match="timeout"):
        summarizer._call_openai_llm({}, "test", "course", "text")
