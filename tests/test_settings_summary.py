import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import LLMProvider
from src.preferences import Preferences, SECRET_FIELDS, defaults, migrate_dashscope_settings, runtime_environment
from src.summarizer import Summarizer, TruncatedSummaryError


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, key, value):
        self.values[service, key] = value

    def get_password(self, service, key):
        return self.values.get((service, key))


def test_secrets_saved_in_single_local_file_without_keychain(tmp_path):
    path = tmp_path / "settings.json"
    class UnusedKeyring:
        def get_password(self, *_):
            pytest.fail("Local settings must not read Keychain")
        def set_password(self, *_):
            pytest.fail("Local settings must not write Keychain")
    store = Preferences(path, UnusedKeyring())
    # Synthetic values exist only during this test; no real account is contacted.
    fake_password = uuid4().hex
    fake_api_key = uuid4().hex
    values = defaults()
    values.update(uis_psw=fake_password, llm_api_key_1=fake_api_key,
                  asr_api_key=uuid4().hex, asr_dashscope_api_key=uuid4().hex)
    store.save(values)
    text = path.read_text()
    assert all(json.loads(text)[field] == values[field] for field in SECRET_FIELDS)
    assert json.loads(text)["credential_storage"] == "file"
    assert "secret_profile" not in json.loads(text)
    assert all(store.load()[field] == values[field] for field in SECRET_FIELDS)
    assert list(tmp_path.iterdir()) == [path]


def test_keychain_import_failure_preserves_old_file(tmp_path):
    class FailingKeyring(MemoryKeyring):
        def get_password(self, *_):
            raise RuntimeError("locked")
    path = tmp_path / "prefs.json"
    path.write_text(json.dumps(dict(schema=1, secret_profile="old", course_ids="12345")))
    previous = path.read_bytes()
    with pytest.raises(RuntimeError, match="locked"):
        Preferences(path, FailingKeyring()).load()
    assert path.read_bytes() == previous


def test_keychain_import_happens_only_once_and_keeps_existing_items(tmp_path):
    from src.preferences import SERVICE
    vault = MemoryKeyring()
    secrets = {field: uuid4().hex for field in SECRET_FIELDS}
    for field, value in secrets.items():
        vault.set_password(SERVICE, f"old:{field}", value)
    before = dict(vault.values)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(dict(schema=1, secret_profile="old", course_ids="12345")))
    store = Preferences(path, vault)
    loaded = store.load()
    assert all(loaded[field] == value for field, value in secrets.items())
    assert loaded['course_ids'] == '12345' and vault.values == before
    assert json.loads(path.read_text())['credential_storage'] == 'file'
    vault.values.clear()
    assert Preferences(path, vault).load() == loaded


def test_failed_atomic_save_keeps_previous_settings_and_credentials(tmp_path, monkeypatch):
    path = tmp_path / 'settings.json'
    store = Preferences(path)
    values = dict(defaults(), uis_psw=uuid4().hex)
    store.save(values)
    previous = path.read_bytes()
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr('src.artifacts.os.replace', fail)
    with pytest.raises(OSError, match='disk full'):
        store.save(dict(values, uis_psw=uuid4().hex))
    assert path.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [path]


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
    assert "ASR_BACKEND" not in env and env["ASR_MODEL"] == defaults()["asr_model"]
    assert env["PATH"] == "/bin"
    assert base["WHISPER_DEVICE"] == "cuda"


@pytest.mark.parametrize("model", ["fun-asr", "Vendor/Custom-ASR-v7"])
@pytest.mark.parametrize("path", ["/api/v1", "/api/v1/services/audio/asr/transcription"])
def test_saved_legacy_aliyun_configuration_migrates_in_local_file(tmp_path, model, path):
    vault = MemoryKeyring()
    store = Preferences(tmp_path / "settings.json", vault)
    original = {**defaults(), "asr_base_url": "https://dashscope.aliyuncs.com" + path,
                "asr_model": model, "asr_api_key": uuid4().hex,
                "uis_psw": uuid4().hex, "llm_api_key_1": uuid4().hex}
    store.save(original)
    legacy = json.loads(store.path.read_text())
    legacy.pop("asr_provider")
    store.path.write_text(json.dumps(legacy))
    before = store.path.read_bytes()
    loaded = store.load()
    assert store.path.read_bytes() == before  # Loading never modifies the vault or file.
    assert loaded["asr_provider"] == "dashscope"
    assert loaded["asr_dashscope_model"] == model
    assert loaded["asr_dashscope_base_url"] == "https://dashscope.aliyuncs.com/api/v1"
    env = runtime_environment(loaded, {})
    assert env["ASR_PROVIDER"] == "dashscope"
    assert env["ASR_API_KEY"] == original["asr_api_key"]
    store.save(loaded)
    restored = store.load()
    assert restored == loaded
    assert all(restored[k] == original[k] for k in ("uis_psw", "llm_api_key_1", "asr_api_key"))
    assert all(json.loads(store.path.read_text())[k] == original[k] for k in ("uis_psw", "llm_api_key_1", "asr_api_key"))


@pytest.mark.parametrize("changes", [
    {"asr_base_url": "https://api.siliconflow.cn/v1"},
    {"asr_base_url": "https://dashscope.aliyuncs.com.example.invalid/api/v1"},
    {"asr_base_url": "https://dashscope.aliyuncs.com/api/v1?token=x"},
    {"asr_base_url": "https://dashscope.aliyuncs.com:bad/api/v1"},
    {"asr_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    {"asr_model": ""},
    {"asr_dashscope_api_key": "separate-account"},
    {"asr_provider": "dashscope"},
])
def test_migration_preserves_other_providers_and_separate_accounts(changes):
    values = {**defaults(), "asr_base_url": "https://dashscope.aliyuncs.com/api/v1",
              "asr_model": "fun-asr", "asr_api_key": uuid4().hex, **changes}
    original = dict(values)
    migrate_dashscope_settings(values)
    assert values == original


def test_explicit_protocol_and_model_are_never_rewritten_on_load(tmp_path):
    store = Preferences(tmp_path / "settings.json", MemoryKeyring())
    original = {**defaults(), "asr_base_url": "https://dashscope.aliyuncs.com/api/v1",
                "asr_model": "Fun-ASR", "asr_api_key": uuid4().hex}
    store.save(original)
    assert store.load() == original


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
