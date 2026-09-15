import os
import re
from dataclasses import dataclass

STUDENT_ID = os.environ.get("StuId", "")
PASSWORD = os.environ.get("UISPsw", "")

WEBVPN_BASE = "https://webvpn.fudan.edu.cn"
IDP_BASE = "https://id.fudan.edu.cn"
ICOURSE_BASE = "https://icourse.fudan.edu.cn"

WEBVPN_AES_KEY = b"wrdvpnisthebest!"
WEBVPN_AES_IV = b"wrdvpnisthebest!"

TENANT_CODE = "222"
GROUP_CODE = "2095000001"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# LLM (ModelScope OpenAI-compatible API)
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api-inference.modelscope.cn/v1/")
_DEFAULT_LLM_MODELS = (
    "ZhipuAI/GLM-5,"
    "deepseek-ai/DeepSeek-V3.2,"
    "MiniMax/MiniMax-M2.5,"
    "Qwen/Qwen3.5-397B-A17B,"
    "ZhipuAI/GLM-4.7"
)

# Gemini fallback (for content policy bypass)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.environ.get(
    "GEMINI_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta/openai/",
)
_DEFAULT_GEMINI_MODELS = "gemini-2.5-pro,gemini-2.5-flash"
API_TIMEOUT_MS = int(os.environ.get("API_TIMEOUT_MS", "600000"))


def _parse_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class LLMProvider:
    """One OpenAI-compatible provider configuration."""

    name: str
    api_key: str
    base_url: str
    models: list[str]
    api_style: str = "openai"


def _parse_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _infer_api_style(base_url: str, explicit_style: str = "") -> str:
    """Infer provider API style from explicit setting or base URL."""
    style = explicit_style.strip().lower()
    if style in {"openai", "anthropic"}:
        return style

    url = base_url.strip().lower()
    if "api/coding" in url or "anthropic" in url:
        return "anthropic"
    return "openai"


LLM_MODELS = _parse_csv(os.environ.get("LLM_MODELS", _DEFAULT_LLM_MODELS))
GEMINI_MODELS = _parse_csv(os.environ.get("GEMINI_MODELS", _DEFAULT_GEMINI_MODELS))


def _build_llm_providers() -> list[LLMProvider]:
    """Build LLM providers from indexed env vars + legacy vars.

    Preferred (supports multiple providers and custom base URL):
      - LLM_API_KEY_1, LLM_BASE_URL_1, LLM_MODELS_1[, LLM_NAME_1]
      - LLM_API_KEY_2, LLM_BASE_URL_2, LLM_MODELS_2[, LLM_NAME_2]
      - ...
    """
    providers: list[LLMProvider] = []

    # 1) Indexed providers (highest priority)
    index_pattern = re.compile(r"^LLM_API_KEY_(\d+)$")
    indices = sorted(
        {
            int(match.group(1))
            for key in os.environ
            for match in [index_pattern.match(key)]
            if match
        }
    )

    for idx in indices:
        api_key = os.environ.get(f"LLM_API_KEY_{idx}", "").strip()
        base_url = os.environ.get(f"LLM_BASE_URL_{idx}", "").strip()
        models = _parse_csv(os.environ.get(f"LLM_MODELS_{idx}", ""))
        name = os.environ.get(f"LLM_NAME_{idx}", f"custom_{idx}").strip()
        api_style = _infer_api_style(
            base_url,
            os.environ.get(f"LLM_API_STYLE_{idx}", ""),
        )

        if not api_key or not base_url or not models:
            # Skip incomplete provider blocks silently for robustness.
            continue

        providers.append(
            LLMProvider(
                name=name,
                api_key=api_key,
                base_url=base_url,
                models=models,
                api_style=api_style,
            )
        )

    # 1.5) Claude/Anthropic-compatible single provider (for local tools)
    anthropic_auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    anthropic_base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "").strip()
    if anthropic_auth_token and anthropic_base_url:
        default_models = ",".join(
            [
                anthropic_model,
                os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "").strip(),
                os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "").strip(),
                os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "").strip(),
            ]
        )
        models = _parse_csv(default_models)
        if models:
            providers.append(
                LLMProvider(
                    name="anthropic",
                    api_key=anthropic_auth_token,
                    base_url=anthropic_base_url,
                    models=models,
                    api_style="anthropic",
                )
            )

    # 2) Legacy Gemini (kept for backward compatibility)
    if GEMINI_API_KEY:
        providers.append(
            LLMProvider(
                name="gemini",
                api_key=GEMINI_API_KEY,
                base_url=GEMINI_BASE_URL,
                models=list(GEMINI_MODELS),
                api_style="openai",
            )
        )

    # 3) Legacy ModelScope (kept for backward compatibility)
    if DASHSCOPE_API_KEY:
        providers.append(
            LLMProvider(
                name="modelscope",
                api_key=DASHSCOPE_API_KEY,
                base_url=LLM_BASE_URL,
                models=list(LLM_MODELS),
                api_style="openai",
            )
        )

    return providers


LLM_PROVIDERS = _build_llm_providers()

# QQ SMTP
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "")
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

# Database & Storage
DATA_DIR = os.environ.get("DATA_DIR", "data")
VIDEO_DIR = os.path.join(DATA_DIR, "videos")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "icourse.db"))

# 监控的课程 ID 列表
COURSE_IDS = [
    c.strip()
    for c in os.environ.get("COURSE_IDS", "").split(",")
    if c.strip()
]
