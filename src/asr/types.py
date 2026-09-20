"""Cloud speech configuration. Credentials never participate in cache metadata."""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from urllib.parse import urlsplit, urlunsplit

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "XingChenAGI/XingChenASR-V3.2-Ultra"
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
DASHSCOPE_DEFAULT_MODEL = "fun-asr"
DASHSCOPE_MODEL_SUGGESTIONS = ("fun-asr", "paraformer-v2")  # Suggestions, never a whitelist.


def is_dashscope_host(hostname):
    return (hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}
            or (hostname or "").endswith(".maas.aliyuncs.com"))


@dataclass(frozen=True)
class ASRSettings:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = field(default="", repr=False, compare=False)
    model: str = DEFAULT_MODEL
    language: str = ""
    initial_prompt: str = ""
    response_format: str = ""
    chunk_seconds: int = 300
    max_upload_mb: int = 20
    timeout_seconds: int = 300
    retries: int = 2
    backend: str = "cloud"
    revision: str = ""
    provider: str = "openai"
    timestamp_alignment: str = "default"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        provider = env.get("ASR_PROVIDER", "openai").strip()
        model = env.get("ASR_MODEL", DASHSCOPE_DEFAULT_MODEL if provider == "dashscope" else DEFAULT_MODEL).strip()
        # Old local model choices cannot accidentally be sent to a cloud vendor.
        if env.get("ASR_BACKEND") in {"auto", "mlx", "cpu", "cuda"}:
            model = DEFAULT_MODEL
        return cls(
            provider=provider,
            base_url=env.get("ASR_BASE_URL", DASHSCOPE_BASE_URL if provider == "dashscope" else DEFAULT_BASE_URL).strip(),
            api_key=env.get("ASR_API_KEY", "").strip(), model=model,
            language=env.get("ASR_LANGUAGE", "").strip(),
            initial_prompt=env.get("ASR_INITIAL_PROMPT", "").strip(),
            response_format=env.get("ASR_RESPONSE_FORMAT", "").strip(),
            chunk_seconds=int(env.get("ASR_CHUNK_SECONDS", "300")),
            max_upload_mb=int(env.get("ASR_MAX_UPLOAD_MB", "20")),
            timeout_seconds=int(env.get("ASR_TIMEOUT_SECONDS", "300")),
            retries=int(env.get("ASR_RETRIES", "2")),
            timestamp_alignment=env.get("ASR_TIMESTAMP_ALIGNMENT", "default").strip(),
        ).resolved()

    def resolved(self):
        url = urlsplit(self.base_url.strip().rstrip("/"))
        if (not url.hostname or url.username or url.password or url.query or url.fragment or
                (url.scheme != "https" and not (url.scheme == "http" and
                 url.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("转录服务地址必须是 HTTPS 地址，不能包含密码、查询参数或片段。")
        path = url.path.rstrip("/")
        if self.provider not in {"openai", "dashscope"}:
            raise ValueError("请选择兼容语音服务或阿里云百炼。")
        if self.provider == "dashscope":
            if path.endswith("/compatible-mode/v1"):
                raise ValueError("百炼录音文件接口不能使用 /compatible-mode/v1，请填写原生接口基础地址（通常为 /api/v1）。")
            if path.endswith("/services/audio/asr/transcription"):
                path = path[:-len("/services/audio/asr/transcription")]
            if not path:
                path = "/api/v1"
            if self.initial_prompt.strip():
                raise ValueError("阿里云录音识别不使用兼容接口的 prompt，请清空专业术语字段。")
        else:
            if is_dashscope_host(url.hostname) and (path == "/api/v1" or path.endswith("/services/audio/asr/transcription")):
                raise ValueError("这是百炼原生录音接口，请将语音服务切换为“百炼录音文件接口”，不要选择兼容接口。")
            if path.endswith("/audio/transcriptions"):
                path = path[:-len("/audio/transcriptions")]
        if self.timestamp_alignment not in {"default", "enabled", "disabled"}:
            raise ValueError("时间戳校准请选择服务默认、开启或关闭。")
        if self.provider != "dashscope" and self.timestamp_alignment != "default":
            raise ValueError("时间戳校准选项仅用于百炼录音文件接口。")
        if not self.model.strip():
            raise ValueError("请填写语音服务提供的模型名称。")
        if self.backend != "cloud":
            raise ValueError("本地转录已移除，请配置云端语音服务。")
        for name, value, low, high in (
            ("音频块时长", self.chunk_seconds, 30, 1800),
            ("上传大小", self.max_upload_mb, 1, 100),
            ("请求超时", self.timeout_seconds, 10, 3600),
            ("重试次数", self.retries, 0, 5),
        ):
            if not low <= value <= high:
                raise ValueError(f"{name}必须在 {low}–{high} 之间。")
        if self.response_format not in {"", "json", "verbose_json", "text", "srt"}:
            raise ValueError("不支持该转录返回格式。")
        return replace(self, base_url=urlunsplit((url.scheme, url.netloc, path, "", "")),
                       model=self.model.strip(), api_key=self.api_key.strip())

    def public_dict(self):
        return {key: value for key, value in asdict(self).items() if key != "api_key"}

    @property
    def fingerprint(self):
        value = self.public_dict()
        if self.provider == "openai":
            value.pop("provider")  # Preserve existing compatible-provider chunk caches.
            value.pop("timestamp_alignment")
        # Changing a key or a transport retry budget must not re-bill completed audio.
        for key in ("timeout_seconds", "retries"):
            value.pop(key)
        value["protocol_version"] = 2  # Phase-safe audio must not reuse old downmix results.
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    segments: tuple[Segment, ...]
    language: str
    decoded_duration: float
    source_duration: float | None
    backend: str
    model: str
    model_revision: str
    complete: bool
    elapsed_seconds: float
    settings_fingerprint: str
    inference_seconds: float = 0.0
    peak_memory_bytes: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self):
        return asdict(self)
