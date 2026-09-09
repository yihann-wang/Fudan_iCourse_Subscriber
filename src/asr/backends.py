"""Heavy dependencies are imported only inside the ASR worker."""

import os
import sys
from pathlib import Path

from .types import ASRSettings


class MLXBackend:
    def __init__(self, settings: ASRSettings):
        import mlx.core as mx
        from huggingface_hub import snapshot_download
        from mlx_whisper.transcribe import ModelHolder

        self.settings = settings
        self.revision = settings.revision
        path = Path(settings.model).expanduser()
        if path.is_dir():
            self.path = str(path.resolve())
        else:
            cached = None
            if settings.revision:
                try:
                    candidate = Path(snapshot_download(settings.model, revision=settings.revision,
                                                       local_files_only=True))
                    if (candidate / "config.json").is_file() and any(
                            (candidate / f).is_file() for f in ("weights.npz", "weights.safetensors")):
                        cached = str(candidate)
                except FileNotFoundError:
                    pass
            self.path = cached or snapshot_download(
                settings.model, revision=settings.revision or None,
                allow_patterns=["config.json", "weights.npz", "weights.safetensors"],
                local_files_only=settings.offline,
            )
            self.revision = Path(self.path).name
        dtype = mx.float32 if settings.compute_type == "float32" else mx.float16
        ModelHolder.get_model(self.path, dtype)

    def transcribe(self, audio):
        import mlx_whisper
        # mlx-whisper 0.4.3 rejects beam_size (including 1) and has no
        # vad_filter argument. Use only options supported by this backend.
        result = mlx_whisper.transcribe(
            audio, path_or_hf_repo=self.path, language=self.settings.language or None,
            fp16=self.settings.compute_type != "float32", verbose=None,
            initial_prompt=self.settings.initial_prompt or None,
            condition_on_previous_text=False,
        )
        return result.get("segments", []), result.get("language", self.settings.language)


class FasterWhisperBackend:
    def __init__(self, settings: ASRSettings):
        import ctranslate2
        from faster_whisper import WhisperModel

        self.settings = settings
        self.revision = settings.revision
        # Keep DLL directory handles alive for the lifetime of the model.
        self._dll_handles = []
        if os.name == "nt" and settings.backend == "cuda":
            for package in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime"):
                try:
                    module = __import__(package, fromlist=["_"])
                    for root in module.__path__:
                        folder = Path(root) / "bin"
                        if folder.is_dir():
                            self._dll_handles.append(os.add_dll_directory(str(folder)))
                except ImportError:
                    pass
        supported = ctranslate2.get_supported_compute_types(settings.backend)
        if settings.compute_type != "auto" and settings.compute_type not in supported:
            raise ValueError(f"{settings.backend} 不支持精度 {settings.compute_type}；支持：{sorted(supported)}")
        model = settings.model
        if getattr(sys, "frozen", False):
            bundled = Path(getattr(sys, "_MEIPASS", "")) / "whisper_model"
            if (bundled / "model.bin").is_file():
                model = str(bundled)
        if not Path(model).is_dir():
            from faster_whisper.utils import download_model
            model = download_model(model, revision=settings.revision or None,
                                   local_files_only=settings.offline)
        if Path(model).parent.name == "snapshots":
            self.revision = Path(model).name
        self.model = WhisperModel(model, device=settings.backend,
                                  compute_type=settings.compute_type,
                                  local_files_only=settings.offline)

    def transcribe(self, audio):
        segments, info = self.model.transcribe(
            audio, language=self.settings.language or None,
            beam_size=max(1, self.settings.beam_size), vad_filter=self.settings.vad_filter,
            initial_prompt=self.settings.initial_prompt or None,
            condition_on_previous_text=False,
        )
        return [dict(start=s.start, end=s.end, text=s.text) for s in segments], info.language


def create_backend(settings):
    return MLXBackend(settings) if settings.backend == "mlx" else FasterWhisperBackend(settings)
