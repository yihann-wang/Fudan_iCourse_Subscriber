"""Backend-neutral ASR configuration and result types (no GPU imports)."""

from .types import ASRSettings, Segment, TranscriptionResult

__all__ = ["ASRSettings", "Segment", "TranscriptionResult"]
