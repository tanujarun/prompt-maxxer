"""Microphone capture.

The stream is opened once at startup and never closed. Opening a WASAPI
capture device costs 100-300 ms, and paying that on key-down is the single
most common reason a dictation app feels sluggish: the first syllable is
already gone by the time the device is live.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import numpy as np
import sounddevice as sd

from .spectrum import BandAnalyzer

log = logging.getLogger(__name__)


class Recorder:
    def __init__(
        self,
        sample_rate: int = 16000,
        block_ms: int = 20,
        device: int | str | None = None,
        on_level: Callable[[float, float, list[float]], None] | None = None,
        level_fps: int = 30,
    ) -> None:
        self.target_rate = sample_rate
        self.block_ms = block_ms
        self.device = device
        self._on_level = on_level
        self._level_interval = 1.0 / max(level_fps, 1)
        self._last_level_at = 0.0

        # Set once the stream is open: the rate the device actually gave us,
        # which may not be the rate we asked for.
        self.stream_rate = sample_rate
        self._resample_needed = False

        self._stream: sd.InputStream | None = None
        # Voice bands for the settings visualizer. Created once the stream's
        # real sample rate is known.
        self._analyzer: BandAnalyzer | None = None
        self._lock = threading.Lock()
        self._capturing = False
        self._chunks: list[np.ndarray] = []
        self._started_at = 0.0

    # -- lifecycle -------------------------------------------------------

    def open(self) -> None:
        """Open the capture stream, falling back to the device default rate."""
        blocksize = int(self.target_rate * self.block_ms / 1000)
        try:
            self._stream = sd.InputStream(
                samplerate=self.target_rate,
                blocksize=blocksize,
                device=self.device,
                channels=1,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            self.stream_rate = self.target_rate
        except Exception as exc:
            log.warning(
                "device refused %d Hz (%s); falling back to its native rate",
                self.target_rate,
                exc,
            )
            info = sd.query_devices(self.device, "input")
            native = int(info["default_samplerate"])
            self._stream = sd.InputStream(
                samplerate=native,
                blocksize=int(native * self.block_ms / 1000),
                device=self.device,
                channels=1,
                dtype="float32",
                callback=self._callback,
            )
            self._stream.start()
            self.stream_rate = native
            self._resample_needed = native != self.target_rate

        self._analyzer = BandAnalyzer(self.stream_rate)

        log.info(
            "capture open: %d Hz%s",
            self.stream_rate,
            f" (resampling to {self.target_rate})" if self._resample_needed else "",
        )

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    # -- audio thread ----------------------------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            log.debug("audio status: %s", status)

        mono = indata[:, 0]
        analyzer = self._analyzer
        if analyzer is not None:
            analyzer.feed(mono)

        with self._lock:
            if self._capturing:
                self._chunks.append(mono.copy())

        if self._on_level is not None:
            now = time.monotonic()
            if now - self._last_level_at >= self._level_interval:
                self._last_level_at = now
                rms = float(np.sqrt(np.mean(np.square(mono))))
                # Peak alongside RMS: peaks follow consonants and plosives, which
                # is what makes the settings scope read as speech.
                peak = float(np.max(np.abs(mono))) if mono.size else 0.0
                bands = analyzer.bands() if analyzer is not None else []
                try:
                    self._on_level(rms, peak, bands)
                except Exception:
                    log.exception("level observer failed")

    # -- capture window --------------------------------------------------

    def begin(self) -> None:
        with self._lock:
            self._chunks = []
            self._capturing = True
            self._started_at = time.monotonic()

    def snapshot(self) -> tuple[np.ndarray, float]:
        """Audio captured so far, without interrupting the recording.

        Used for live partial transcripts: the streaming worker needs a
        consistent copy of a buffer the audio thread is still appending to.
        """
        with self._lock:
            if not self._capturing or not self._chunks:
                return np.zeros(0, dtype=np.float32), 0.0
            # Copy the list, not the arrays: each chunk is already immutable
            # once the callback has appended it.
            chunks = list(self._chunks)
            duration = time.monotonic() - self._started_at
        return self._assemble(chunks), duration

    def end(self) -> tuple[np.ndarray, float]:
        """Stop capturing and return (mono float32 at target_rate, duration_s)."""
        with self._lock:
            self._capturing = False
            chunks = self._chunks
            self._chunks = []
            duration = time.monotonic() - self._started_at
        return self._assemble(chunks), duration

    def _assemble(self, chunks: list[np.ndarray]) -> np.ndarray:
        if not chunks:
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(chunks).astype(np.float32, copy=False)

        if self._resample_needed:
            import soxr

            audio = soxr.resample(audio, self.stream_rate, self.target_rate)

        return audio

    @property
    def capturing(self) -> bool:
        return self._capturing


def list_input_devices() -> list[dict]:
    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            devices.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "default_samplerate": dev["default_samplerate"],
                }
            )
    return devices
