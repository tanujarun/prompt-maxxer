"""The contract every ASR backend implements.

Keeping this narrow is what lets Parakeet TDT (faster, better English WER) drop
in later without the pipeline, hotkey or injection code changing at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass
class Transcript:
    text: str
    language: str | None = None
    # Wall-clock seconds spent inside the model, for the latency HUD.
    latency_s: float = 0.0
    audio_s: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def realtime_factor(self) -> float:
        return self.audio_s / self.latency_s if self.latency_s > 0 else 0.0


class AsrEngine(Protocol):
    name: str

    def load(self) -> None:
        """Load weights and run a warm-up pass. Called once at startup."""

    def transcribe(
        self, audio: np.ndarray, sample_rate: int, partial: bool = False
    ) -> Transcript:
        """Transcribe mono float32 audio in [-1, 1].

        `partial` marks a live in-progress pass over an incomplete utterance:
        the backend should trade accuracy for latency, since the result is
        only shown, never typed.
        """

    def unload(self) -> None: ...
