"""faster-whisper (CTranslate2) backend.

Chosen over whisper.cpp because CTranslate2 ships prebuilt CUDA wheels: it
picks up cuBLAS and cuDNN from the nvidia-* pip packages, so there is no CUDA
toolkit and no nvcc in the build path.
"""

from __future__ import annotations

import logging
import time

import numpy as np

from ..models import revision_for
from .base import Transcript

log = logging.getLogger(__name__)


class FasterWhisperEngine:
    name = "faster-whisper"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._model = None
        # Where the model actually ended up: CUDA can fail and fall back to CPU.
        self.device_in_use = cfg.device

    def load(self) -> None:
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        log.info(
            "loading %s on %s (%s)", self.cfg.model, self.cfg.device, self.cfg.compute_type
        )
        try:
            self._model = WhisperModel(
                self.cfg.model,
                device=self.cfg.device,
                compute_type=self.cfg.compute_type,
                # Pinned, so the model on disk is exactly the one tested.
                revision=revision_for(self.cfg.model),
            )
            self.device_in_use = self.cfg.device
        except Exception:
            if self.cfg.device != "cuda":
                raise
            log.exception("CUDA load failed; falling back to CPU int8")
            self._model = WhisperModel(
                self.cfg.model, device="cpu", compute_type="int8",
                revision=revision_for(self.cfg.model),
            )
            self.device_in_use = "cpu"

        # Warm-up. The first call pays for CUDA context creation, kernel
        # autotuning and graph capture; without this the user's first
        # dictation of the session is several seconds slower than every
        # subsequent one.
        # Two passes, not one: the first still pays CUDA autotuning, and
        # measuring after only one warm pass overstates steady-state latency
        # by roughly 3x.
        silence = np.zeros(self.cfg_sample_rate, dtype=np.float32)
        for _ in range(2):
            list(
                self._model.transcribe(
                    silence, beam_size=1, language=self.cfg.language or "en"
                )[0]
            )
        log.info("model ready in %.1fs", time.perf_counter() - t0)

    @property
    def cfg_sample_rate(self) -> int:
        return 16000

    def transcribe(
        self, audio: np.ndarray, sample_rate: int, partial: bool = False
    ) -> Transcript:
        if self._model is None:
            raise RuntimeError("transcribe() called before load()")
        if sample_rate != 16000:
            raise ValueError(f"expected 16 kHz audio, got {sample_rate}")

        # A partial runs over a buffer that is still growing and will be
        # thrown away, so it skips VAD and never widens the beam.
        beam_size = 1 if partial else self.cfg.beam_size
        vad_filter = False if partial else self.cfg.vad_filter

        t0 = time.perf_counter()
        segments, info = self._model.transcribe(
            audio,
            language=self.cfg.language,
            beam_size=beam_size,
            vad_filter=vad_filter,
            # Dictation utterances are independent. Conditioning on previous
            # text is the main driver of Whisper's repetition-loop failure.
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = "".join(seg.text for seg in segments).strip()
        latency = time.perf_counter() - t0

        return Transcript(
            text=text,
            language=getattr(info, "language", None),
            latency_s=latency,
            audio_s=len(audio) / sample_rate,
            meta={
                "model": self.cfg.model,
                "compute_type": self.cfg.compute_type,
                "partial": partial,
            },
        )

    def unload(self) -> None:
        self._model = None
        # CTranslate2 releases VRAM when the model object is collected. Force
        # it, so switching models frees the old one before loading the next.
        import gc

        gc.collect()
