import hashlib
import json
import os
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..artifacts import cached_file_sha256

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
MLX_REVISION = "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb"
CPU_REVISION = "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"


@dataclass(frozen=True)
class ASRSettings:
    backend: str = "auto"
    model: str = "large-v3-turbo"
    revision: str = ""
    language: str = "zh"
    compute_type: str = "auto"
    beam_size: int = 5
    vad_filter: bool = True
    chunk_seconds: int = 300
    overlap_seconds: float = 2.0
    initial_prompt: str = ""
    offline: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        backend = env.get("ASR_BACKEND", "auto").strip().lower()
        # Old CUDA settings must not select a CUDA runtime on Apple Silicon.
        if backend == "auto" and env.get("WHISPER_DEVICE") == "cpu":
            backend = "cpu"
        elif backend == "auto" and platform.system() != "Darwin":
            backend = env.get("WHISPER_DEVICE", "auto").strip().lower()
        return cls(
            backend=backend,
            model=env.get("ASR_MODEL", env.get("WHISPER_MODEL", "large-v3-turbo")).strip(),
            revision=env.get("ASR_MODEL_REVISION", "").strip(),
            language=env.get("WHISPER_LANGUAGE", "zh").strip(),
            compute_type=env.get("WHISPER_COMPUTE_TYPE", "auto").strip(),
            beam_size=int(env.get("WHISPER_BEAM_SIZE", "5")),
            vad_filter=env.get("WHISPER_VAD_FILTER", "true").lower() in {"1", "true", "yes"},
            chunk_seconds=int(env.get("ASR_CHUNK_SECONDS", "300")),
            overlap_seconds=float(env.get("ASR_OVERLAP_SECONDS", "2")),
            initial_prompt=env.get("ASR_INITIAL_PROMPT", ""),
            offline=env.get("HF_HUB_OFFLINE", "0").lower() in {"1", "true", "yes"},
        ).resolved()

    def resolved(self):
        values = asdict(self)
        apple = platform.system() == "Darwin" and platform.machine() == "arm64"
        backend = self.backend
        if backend == "auto":
            backend = "mlx" if apple else "cpu"
        if backend not in {"mlx", "cpu", "cuda"}:
            raise ValueError("ASR_BACKEND 必须是 auto、mlx、cpu 或 cuda；CTranslate2 不支持 mps。")
        if backend == "mlx" and not apple:
            raise ValueError("MLX 后端需要 Apple Silicon Mac。请使用 ASR_BACKEND=cpu。")
        if backend == "cuda" and platform.system() == "Darwin":
            raise ValueError("macOS 不支持 CUDA；请使用 ASR_BACKEND=mlx 或 cpu。")
        if not 30 <= self.chunk_seconds <= 1800:
            raise ValueError("ASR_CHUNK_SECONDS 必须在 30–1800 秒之间。")
        if not 0 <= self.overlap_seconds <= min(10, self.chunk_seconds / 4):
            raise ValueError("ASR_OVERLAP_SECONDS 必须在 0–10 秒之间。")
        model = self.model
        if not model:
            raise ValueError("转录模型不能为空。")
        if backend == "mlx":
            values["beam_size"] = 1
            values["vad_filter"] = False
            if model == "large-v3-turbo":
                model = MLX_MODEL
            elif "/" not in model and not Path(model).is_dir():
                model = "mlx-community/whisper-" + model
            values["compute_type"] = "float32" if self.compute_type == "float32" else "float16"
            if model == MLX_MODEL and not self.revision:
                values["revision"] = MLX_REVISION
        elif backend == "cpu" and self.compute_type in {"auto", "int8_float16", "float16"}:
            values["compute_type"] = "int8"
        if backend in {"cpu", "cuda"} and model == "large-v3-turbo" and not self.revision:
            values["revision"] = CPU_REVISION
        values.update(backend=backend, model=model)
        return ASRSettings(**values)

    @property
    def fingerprint(self):
        value = asdict(self)
        model = Path(self.model).expanduser()
        if model.is_dir():
            value["local_model_files"] = {
                p.name: cached_file_sha256(p) for p in model.iterdir()
                if p.is_file() and p.suffix in {".json", ".bin", ".npz", ".safetensors"}
            }
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
