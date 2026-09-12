"""Pluggable speech recognition backends."""

from .base import AsrEngine, Transcript

__all__ = ["AsrEngine", "Transcript", "build_engine"]


def build_engine(cfg) -> AsrEngine:
    """Construct the engine named by cfg.engine."""
    if cfg.engine == "faster-whisper":
        from .faster_whisper_engine import FasterWhisperEngine

        return FasterWhisperEngine(cfg)
    if cfg.engine == "parakeet":
        raise NotImplementedError(
            "The Parakeet backend is a planned swap-in for English-only use; "
            "it is not wired up yet."
        )
    raise ValueError(f"unknown ASR engine: {cfg.engine!r}")
