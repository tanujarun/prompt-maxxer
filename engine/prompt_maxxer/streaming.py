"""Live partial transcripts while the key is held.

Whisper has no streaming mode. The trick every "live dictation" app uses is to
re-run it over the whole buffer captured so far, on a timer, and show the newest
result. It is quadratic work in principle, but the constant is tiny here: a 3 s
buffer costs ~110 ms on this GPU, so even several passes a second leave the card
mostly idle.

Two things make the result feel solid rather than noisy:

* **Partials never get typed.** They are display-only. The text that lands in
  your editor is always the single full-quality pass taken after release, so a
  wrong guess mid-sentence costs nothing.
* **Stable and tentative text are separated.** Re-transcribing a growing buffer
  makes the tail churn as context arrives ("what if I have a lot" -> "what if I
  have a lot of limit"). Words that survived the previous pass unchanged are
  marked stable; the rest is tentative and the overlay dims it. That is the
  LocalAgreement idea from the whisper-streaming literature, at its simplest.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

log = logging.getLogger(__name__)


# Word characters are anything that is not whitespace or a hyphen. Splitting
# on hyphens matters: Whisper flips between "push to talk" and "push-to-talk"
# between passes, and a plain word-wise comparison sees that as three words
# changing into one, which invalidates the entire rest of the sentence.
_WORD = re.compile(r"[^\s\-‐-―]+")
_TRIM = ".,!?;:\"'()[]"


def _tokens(text: str) -> list[tuple[str, int]]:
    """Normalised comparison tokens, each with where it ends in `text`."""
    out: list[tuple[str, int]] = []
    for match in _WORD.finditer(text):
        token = match.group(0).strip(_TRIM).lower()
        if token:
            out.append((token, match.end()))
    return out


def split_stable(previous: str, current: str) -> tuple[str, str]:
    """Split `current` into the prefix that agrees with `previous`, and the rest.

    Comparison is on normalised tokens so that punctuation and hyphenation
    churn does not destabilise text that has not really changed; the returned
    strings are slices of the original `current`, so display is unaffected.
    """
    prev_tokens = _tokens(previous)
    cur_tokens = _tokens(current)

    agreed = 0
    while (
        agreed < len(prev_tokens)
        and agreed < len(cur_tokens)
        and prev_tokens[agreed][0] == cur_tokens[agreed][0]
    ):
        agreed += 1

    if agreed == 0:
        return "", current.strip()

    cut = cur_tokens[agreed - 1][1]
    return current[:cut].strip(), current[cut:].strip()


class StreamingTranscriber:
    """Runs partial passes while recording, and publishes them."""

    def __init__(self, pipeline) -> None:
        self._p = pipeline
        self._cfg = pipeline.cfg.streaming
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous = ""
        self.latest = ""

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not self._cfg.enabled:
            return
        self.stop()
        self._stop.clear()
        self._previous = ""
        self.latest = ""
        self._thread = threading.Thread(
            target=self._loop, name="prompt-maxxer-streaming", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Ask the worker to stop issuing passes.

        Deliberately does not join: an in-flight pass holds the model lock, and
        the release path wants to queue up behind it rather than block the
        hotkey thread waiting for it to finish.
        """
        self._stop.set()

    # -- worker ----------------------------------------------------------

    def _loop(self) -> None:
        interval = self._cfg.interval_ms / 1000
        last_audio_s = 0.0

        while not self._stop.is_set():
            if self._stop.wait(interval):
                return

            audio, duration = self._p.recorder.snapshot()
            if audio.size == 0 or duration < self._cfg.min_audio_s:
                continue

            # If the model is running slower than the interval, there is no
            # point queueing a pass over almost the same audio.
            if duration - last_audio_s < self._cfg.min_growth_s:
                continue

            # Same silence guard as the final pass: never let the model invent
            # words out of room tone.
            rms = float(np.sqrt(np.mean(np.square(audio))))
            if rms < self._p.cfg.audio.min_rms:
                continue

            if self._stop.is_set():
                return

            try:
                self._pass(audio, duration)
            except Exception:
                log.exception("partial transcription failed")
                return

            last_audio_s = duration

    def _pass(self, audio: np.ndarray, duration: float) -> None:
        started = time.perf_counter()

        # Serialised against the final pass: one model, one GPU stream.
        with self._p.model_lock:
            if self._stop.is_set():
                return
            result = self._p.asr.transcribe(
                audio, self._p.cfg.audio.sample_rate, partial=True
            )

        text = result.text.strip()
        if not text:
            return

        stable, tentative = split_stable(self._previous, text)
        self._previous = text
        self.latest = text

        log.debug(
            "partial %.1fs -> %.0fms: %s",
            duration,
            (time.perf_counter() - started) * 1000,
            text[:60],
        )

        # A late partial must not overwrite the final transcript in the UI.
        if self._stop.is_set():
            return

        self._p.bus.emit(
            "partial",
            stable=stable,
            tentative=tentative,
            audio_s=round(duration, 2),
            latency_ms=round(result.latency_s * 1000),
        )
