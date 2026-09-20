"""Ordinary preferences in Application Support; credentials in Keychain."""

import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from platformdirs import user_config_path

from .artifacts import atomic_write_json
from .asr.types import DASHSCOPE_BASE_URL, DASHSCOPE_MODELS, DEFAULT_BASE_URL, DEFAULT_MODEL
from .summary_settings import DEFAULT_OUTPUT_TOKENS, DEFAULT_TIMEOUT_MINUTES

SERVICE = "Fudan iCourse Subscriber"
SECRET_FIELDS = ("uis_psw", "llm_api_key_1", "asr_api_key", "asr_dashscope_api_key")


def defaults():
    home = Path.home() / "iCourse"
    return dict(mode="download_and_summarize", stu_id="", uis_psw="", course_ids="",
                sub_ids="", skip_time_periods="", out_dir=str(home / "课程"),
                summary_dir=str(home / "笔记"), local_media="", asr_base_url=DEFAULT_BASE_URL, asr_model=DEFAULT_MODEL,
                asr_api_key="", asr_language="", asr_prompt="", asr_response_format="",
                asr_provider="openai", asr_dashscope_base_url=DASHSCOPE_BASE_URL,
                asr_dashscope_model="fun-asr", asr_dashscope_api_key="",
                chunk_seconds=300, asr_max_upload_mb=20, asr_timeout_seconds=300, asr_retries=2,
                llm_name_1="LLM", llm_api_key_1="", llm_base_url_1="", llm_models_1="",
                llm_output_tokens=DEFAULT_OUTPUT_TOKENS,
                llm_timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
                overwrite=False, redo_notes=False, keep_awake=True)


def as_bool(value):
    return value is True or str(value).lower() in {"1", "true", "yes"}


def migrate_dashscope_settings(values):
    """Recognize native file-ASR settings entered in the old compatible fields.

    Only move credentials between fields for the same official API origin.
    This is an in-memory migration; normal transactional save persists it.
    """
    if values.get("asr_provider", "openai") != "openai":
        return
    model = str(values.get("asr_model", "")).strip().lower()
    if model not in DASHSCOPE_MODELS:
        return
    try:
        url = urlsplit(str(values.get("asr_base_url", "")).strip().rstrip("/"))
        official = (url.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
                    or (url.hostname or "").endswith(".maas.aliyuncs.com"))
        if (not official or url.scheme != "https" or url.username or url.password or url.port
                or url.query or url.fragment or url.path not in {
                    "", "/api/v1", "/compatible-mode/v1", "/api/v1/services/audio/asr/transcription"}):
            return
    except ValueError:
        return
    key = values.get("asr_api_key", "")
    existing = values.get("asr_dashscope_api_key", "")
    if existing and existing != key:
        return  # Never replace an independently configured account.
    values.update(asr_provider="dashscope", asr_dashscope_model=model,
                  asr_dashscope_base_url=urlunsplit((url.scheme, url.netloc, "/api/v1", "", "")),
                  asr_dashscope_api_key=key)


class Preferences:
    def __init__(self, path=None, keyring_backend=None):
        self.path = Path(path) if path else user_config_path(SERVICE, appauthor=False) / "settings.json"
        if keyring_backend is None:
            import keyring
            keyring_backend = keyring
        self.keyring = keyring_backend
        self.profile = None

    def load(self, legacy_path=None):
        values = defaults()
        source = self.path if self.path.exists() else Path(legacy_path) if legacy_path else None
        if source and source.exists():
            saved = json.loads(source.read_text(encoding="utf-8"))
            values.update({k: v for k, v in saved.items() if k in values})
            self.profile = saved.get("secret_profile")
            if self.profile:
                for field in SECRET_FIELDS:
                    values[field] = self.keyring.get_password(SERVICE, f"{self.profile}:{field}") or ""
        for field in ("out_dir", "summary_dir"):
            if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", str(values[field])):
                values[field] = defaults()[field]
        for field in ("overwrite", "redo_notes", "keep_awake"):
            values[field] = as_bool(values[field])
        migrate_dashscope_settings(values)
        return values

    def save(self, values):
        # New vault entries make a save transactional with the settings file:
        # failed disk writes cannot change credentials of the previous profile.
        profile = uuid.uuid4().hex
        # If Keychain fails, no plaintext fallback is written.
        for field in SECRET_FIELDS:
            secret = str(values.get(field, ""))
            self.keyring.set_password(SERVICE, f"{profile}:{field}", secret)
            if self.keyring.get_password(SERVICE, f"{profile}:{field}") != secret:
                raise RuntimeError("系统钥匙串保存验证失败。")
        public = {k: v for k, v in values.items() if k in defaults() and k not in SECRET_FIELDS}
        public.update(schema=1, secret_profile=profile)
        atomic_write_json(self.path, public, private=True)
        self.profile = profile


def runtime_environment(values, base=None):
    env = dict(os.environ if base is None else base)
    # GUI fields are the selected configuration; do not mix old providers from
    # the shell into this profile. subprocess receives its own environment.
    for key in list(env):
        if key.startswith(("LLM_", "WHISPER_", "ASR_", "GEMINI_", "DASHSCOPE_", "ANTHROPIC_")):
            env.pop(key)
    mapping = dict(stu_id="StuId", uis_psw="UISPsw", course_ids="COURSE_IDS",
                   asr_provider="ASR_PROVIDER", asr_base_url="ASR_BASE_URL", asr_model="ASR_MODEL", asr_api_key="ASR_API_KEY",
                   asr_language="ASR_LANGUAGE", asr_prompt="ASR_INITIAL_PROMPT",
                   asr_response_format="ASR_RESPONSE_FORMAT", asr_max_upload_mb="ASR_MAX_UPLOAD_MB",
                   asr_timeout_seconds="ASR_TIMEOUT_SECONDS", asr_retries="ASR_RETRIES",
                   chunk_seconds="ASR_CHUNK_SECONDS", llm_name_1="LLM_NAME_1",
                   llm_api_key_1="LLM_API_KEY_1", llm_base_url_1="LLM_BASE_URL_1", llm_models_1="LLM_MODELS_1")
    for field, key in mapping.items():
        env[key] = str(values.get(field, defaults().get(field, "")))
    if env["ASR_PROVIDER"] == "dashscope":
        for field, key in (("asr_dashscope_base_url", "ASR_BASE_URL"),
                           ("asr_dashscope_model", "ASR_MODEL"), ("asr_dashscope_api_key", "ASR_API_KEY")):
            env[key] = str(values.get(field, defaults()[field]))
        env["ASR_INITIAL_PROMPT"] = ""
        env["ASR_RESPONSE_FORMAT"] = ""  # File ASR always requests its native timed JSON.
    for field, key, scale in (("llm_output_tokens", "LLM_MAX_OUTPUT_TOKENS", 1),
                              ("llm_timeout_minutes", "API_TIMEOUT_MS", 60000)):
        value = int(values.get(field, defaults()[field]))
        if value <= 0:
            raise ValueError("笔记生成参数必须大于零。")
        env[key] = str(value * scale)
    env.update(PYTHONUTF8="1", PYTHONUNBUFFERED="1", ICOURSE_GUI="1", ICOURSE_EVENTS="json")
    return env
