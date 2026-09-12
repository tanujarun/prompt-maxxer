"""The dictation loop, plus the controls the settings window drives.

Key down -> capture -> transcribe -> clean -> inject is the hot path, and its
ordering is deliberate:

* Capture begins on the key-down event itself, with no work in between.
* Transcription runs on a worker thread. If it ran on the hotkey dispatch
  thread, holding the key again during a slow transcription would be ignored.
* State changes are emitted before the work they describe, so the overlay
  reacts immediately rather than after the fact.

Everything else - rebinding the hotkey, the session's tape log, the idle mic
monitor, downloading and switching models - arrives as commands from the event
bus and stays off that path: commands return at once and do slow work on their
own threads.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from . import cuda_runtime, keys, models, system
from . import inject as inject_mod
from .asr import build_engine
from .audio import Recorder
from .cleanup import build_cleaner
from .hotkey import Captured, PushToTalkHook
from .ipc import EventBus
from .streaming import StreamingTranscriber

log = logging.getLogger(__name__)

IDLE = "idle"
LOADING = "loading"
RECORDING = "recording"
TRANSCRIBING = "transcribing"

#: Takes kept for the session. Held in memory only: dictation never touches disk.
MAX_TAKES = 200
#: How long hotkey capture waits for a key before giving up.
CAPTURE_TIMEOUT_S = 12.0


class Pipeline:
    def __init__(self, cfg, config_path: Path | None = None, first_run: bool = False) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self._first_run = first_run
        self.bus = EventBus(
            cfg.ipc.host,
            cfg.ipc.port,
            on_command=self._on_command,
            snapshot=self._snapshot,
        )
        self.recorder = Recorder(
            sample_rate=cfg.audio.sample_rate,
            block_ms=cfg.audio.block_ms,
            device=cfg.audio.device,
            on_level=self._on_level,
            level_fps=cfg.ipc.level_fps,
        )
        self.asr = build_engine(cfg.asr)
        self.cleaner = build_cleaner(cfg.cleanup)
        self.hook = PushToTalkHook(
            vk_code=cfg.hotkey.vk_code,
            on_press=self._on_press,
            on_release=self._on_release,
            on_capture=self._on_capture,
            suppress=cfg.hotkey.suppress,
        )
        self._state = IDLE
        self._ready = False
        self._stop = threading.Event()
        # Serialises transcription and model switches, so two fast utterances
        # cannot interleave their injected text and a switch never lands
        # mid-take.
        self._work_lock = threading.Lock()
        # Guards the model itself. Partial passes and the final pass share one
        # WhisperModel on one GPU stream, so they must not overlap.
        self.model_lock = threading.Lock()
        self.streaming = StreamingTranscriber(self)

        self._takes: deque[dict] = deque(maxlen=MAX_TAKES)
        self._takes_lock = threading.Lock()
        self._take_no = 0

        self._capture_timer: threading.Timer | None = None

        self.system: system.SystemInfo | None = None
        self._downloader = models.Downloader()

    # -- state and snapshot ------------------------------------------------

    def _set_state(self, state: str, **extra) -> None:
        self._state = state
        self.bus.emit("state", state=state, **extra)

    def _ready_payload(self) -> dict:
        return {
            "event": "ready",
            "hotkey": self.cfg.hotkey.label,
            "engine": self.asr.name,
            "model": models.canonical(self.cfg.asr.model),
            "device": getattr(self.asr, "device_in_use", self.cfg.asr.device),
            "streaming": self.cfg.streaming.enabled,
        }

    def _log_payload(self) -> dict:
        with self._takes_lock:
            return {"event": "log", "takes": list(self._takes)}

    def _models_payload(self) -> dict | None:
        if self.system is None:
            return None
        return {
            "event": "models",
            **models.catalog(
                self.system,
                self.cfg.asr.language,
                self.cfg.asr.model,
                downloading=self._downloader.current,
            ),
        }

    def _broadcast(self, payload: dict | None) -> None:
        if payload is not None:
            payload = dict(payload)
            self.bus.emit(payload.pop("event"), **payload)

    def _snapshot(self) -> list[dict]:
        """Everything a window needs to render correctly the moment it connects."""
        payloads = []
        if self._ready:
            payloads.append(self._ready_payload())
        payloads.append({"event": "state", "state": self._state})
        payloads.append(self._log_payload())
        catalog = self._models_payload()
        if catalog is not None:
            payloads.append(catalog)
        return payloads

    def _save_config(self) -> None:
        try:
            self.cfg.save(self.config_path)
        except OSError:
            log.exception("could not save config")

    # -- levels ----------------------------------------------------------

    def _on_level(self, rms: float, peak: float, bands: list[float]) -> None:
        payload = {"rms": round(rms, 5), "peak": round(peak, 5), "bands": bands}
        if self._state == RECORDING:
            self.bus.emit("level", **payload)
        elif self.bus.monitoring:
            # The idle monitor behind the settings visualizer, sent only to the
            # windows that asked, so the overlay is not woken all day.
            self.bus.emit("level", monitors_only=True, **payload)

    # -- hotkey handlers -------------------------------------------------

    def _on_press(self) -> None:
        if self._state in (RECORDING, LOADING):
            return
        self.recorder.begin()
        self._set_state(RECORDING)
        self.streaming.start()
        log.debug("capture started")

    def _on_release(self) -> None:
        if self._state != RECORDING:
            return
        # Stop issuing partials before anything else, so a pass started on the
        # last tick cannot land after the final transcript and overwrite it.
        self.streaming.stop()
        audio, duration = self.recorder.end()

        if duration * 1000 < self.cfg.audio.min_utterance_ms:
            log.debug("ignoring %.0f ms tap", duration * 1000)
            self._set_state(IDLE, reason="too_short")
            return

        # Cheaper than a forward pass, and it prevents the failure mode where
        # a silent room comes back as a confident sentence.
        rms = float(np.sqrt(np.mean(np.square(audio)))) if audio.size else 0.0
        if rms < self.cfg.audio.min_rms:
            log.debug("ignoring silence (rms %.4f < %.4f)", rms, self.cfg.audio.min_rms)
            self._set_state(IDLE, reason="silence")
            return

        self._set_state(TRANSCRIBING, audio_s=duration)
        threading.Thread(
            target=self._transcribe_and_inject,
            args=(audio, duration),
            name="prompt-maxxer-asr",
            daemon=True,
        ).start()

    # -- worker ----------------------------------------------------------

    def _transcribe_and_inject(self, audio, duration: float) -> None:
        with self._work_lock:
            t0 = time.perf_counter()
            try:
                with self.model_lock:
                    result = self.asr.transcribe(audio, self.cfg.audio.sample_rate)
                text = self.cleaner.clean(result.text)

                if not text:
                    self._set_state(IDLE, reason="empty")
                    return

                strategy = inject_mod.inject(text, self.cfg.inject)
                total = time.perf_counter() - t0
                asr_ms = round(result.latency_s * 1000)
                total_ms = round(total * 1000)

                log.info(
                    "%.1fs audio -> %dms asr, %dms total (%.0fx realtime) via %s: %s",
                    duration,
                    asr_ms,
                    total_ms,
                    result.realtime_factor,
                    strategy,
                    text[:60] + ("..." if len(text) > 60 else ""),
                )
                take = self._record_take(text, duration, asr_ms, total_ms, strategy)
                self.bus.emit(
                    "transcript",
                    text=text,
                    language=result.language,
                    audio_s=duration,
                    asr_ms=asr_ms,
                    total_ms=total_ms,
                    strategy=strategy,
                    take=take,
                )
                self._set_state(IDLE)
            except Exception as exc:
                log.exception("dictation failed")
                self.bus.emit("error", message=str(exc))
                self._set_state(IDLE, reason="error")

    # -- commands (event-bus thread: must return immediately) ------------------

    def _on_command(self, cmd: str, message: dict) -> None:
        if cmd == "capture_hotkey":
            self._begin_capture()
        elif cmd == "cancel_capture":
            self.hook.cancel_capture()
        elif cmd == "clear_log":
            self._clear_log()
        elif cmd == "list_models":
            self._broadcast(self._models_payload())
        elif cmd == "download_model":
            self._download_model(str(message.get("model", "")))
        elif cmd == "switch_model":
            self._request_switch(str(message.get("model", "")))
        else:
            log.debug("ignoring unknown command %r", cmd)

    # -- hotkey capture --------------------------------------------------------

    def _begin_capture(self) -> None:
        if self._state != IDLE:
            self.bus.emit("hotkey_capture", status="busy")
            return
        self._cancel_capture_timer()
        self.hook.begin_capture()
        timer = threading.Timer(CAPTURE_TIMEOUT_S, self.hook.cancel_capture)
        timer.daemon = True
        timer.start()
        self._capture_timer = timer
        self.bus.emit("hotkey_capture", status="listening")

    def _cancel_capture_timer(self) -> None:
        if self._capture_timer is not None:
            self._capture_timer.cancel()
            self._capture_timer = None

    def _on_capture(self, captured: Captured) -> None:
        self._cancel_capture_timer()
        if captured is None:
            self.bus.emit("hotkey_capture", status="cancelled")
            return

        vk, scan_code, flags = captured
        label = keys.key_label(vk, scan_code, flags)
        reason = keys.refusal(vk)
        if reason:
            log.info("refused %s as the hotkey: %s", label, reason)
            self.bus.emit("hotkey_capture", status="refused", label=label, reason=reason)
            return

        self.hook.set_key(vk)
        self.cfg.hotkey.vk_code = vk
        self.cfg.hotkey.label = label
        self._save_config()
        log.info("push-to-talk rebound to %s (vk=0x%02X)", label, vk)
        self.bus.emit("hotkey_capture", status="saved", label=label)
        self._broadcast(self._ready_payload())

    # -- tape log --------------------------------------------------------------

    def _record_take(
        self, text: str, duration: float, asr_ms: int, total_ms: int, strategy: str
    ) -> int:
        with self._takes_lock:
            self._take_no += 1
            self._takes.append(
                {
                    "n": self._take_no,
                    "text": text,
                    "audio_s": round(duration, 2),
                    "asr_ms": asr_ms,
                    "total_ms": total_ms,
                    "strategy": strategy,
                }
            )
            return self._take_no

    def _clear_log(self) -> None:
        with self._takes_lock:
            self._takes.clear()
            self._take_no = 0
        log.info("tape log cleared")
        self.bus.emit("log", takes=[])

    # -- models ------------------------------------------------------------------

    def _placement(self) -> dict:
        """Device and precision for a model on this machine."""
        info = self.system
        if info is None:
            return {}
        if info.cuda:
            for compute_type in ("int8_float16", "float16", "int8"):
                if compute_type in info.cuda_compute_types:
                    return {"device": "cuda", "compute_type": compute_type}
            return {"device": "cuda", "compute_type": "default"}
        return {"device": "cpu", "compute_type": "int8"}

    def _download_model(self, model_id: str) -> None:
        if models.spec_for(model_id) is None:
            self.bus.emit("model_download", model=model_id, status="error", message="Unknown model")
            return
        model_id = models.canonical(model_id)
        if models.is_downloaded(model_id):
            self._broadcast(self._models_payload())
            return

        def progress(done: int, total: int | None) -> None:
            self.bus.emit(
                "model_download", model=model_id, status="downloading",
                done_bytes=done, total_bytes=total,
            )

        def finished(error: Exception | None) -> None:
            if error is None:
                self.bus.emit("model_download", model=model_id, status="done")
            else:
                self.bus.emit("model_download", model=model_id, status="error", message=str(error))
            self._broadcast(self._models_payload())

        if not self._downloader.start(model_id, progress, finished):
            self.bus.emit(
                "model_download", model=model_id, status="error",
                message="Another download is already running",
            )
            return
        self.bus.emit(
            "model_download", model=model_id, status="downloading", done_bytes=0, total_bytes=None
        )

    def _request_switch(self, model_id: str) -> None:
        if models.spec_for(model_id) is None:
            self.bus.emit("model_switch", model=model_id, status="error", message="Unknown model")
            return
        model_id = models.canonical(model_id)
        if not models.is_downloaded(model_id):
            self.bus.emit("model_switch", model=model_id, status="error", message="Download it first")
            return
        if model_id == models.canonical(self.cfg.asr.model):
            self.bus.emit("model_switch", model=model_id, status="done")
            return
        if self._state != IDLE:
            self.bus.emit(
                "model_switch", model=model_id, status="error",
                message="Finish the current take first",
            )
            return
        threading.Thread(
            target=self._switch_model, args=(model_id,), name="prompt-maxxer-switch", daemon=True
        ).start()

    def _switch_model(self, model_id: str) -> None:
        with self._work_lock:
            if self._state != IDLE:
                self.bus.emit(
                    "model_switch", model=model_id, status="error",
                    message="Finish the current take first",
                )
                return

            previous = self.cfg.asr
            target = dataclasses.replace(previous, model=model_id, **self._placement())
            self.bus.emit("model_switch", model=model_id, status="loading")
            self._set_state(LOADING)
            started = time.perf_counter()
            try:
                with self.model_lock:
                    # Free the current model first: holding both would need
                    # their combined VRAM, which a smaller card may not have.
                    self.asr.unload()
                    engine = build_engine(target)
                    try:
                        engine.load()
                    except Exception:
                        log.exception("could not load %s; restoring %s", model_id, previous.model)
                        self.asr.load()
                        raise
                    self.asr = engine
                self.cfg.asr = target
                self._save_config()
            except Exception as exc:
                self.bus.emit("model_switch", model=model_id, status="error", message=str(exc))
            else:
                log.info("switched to %s in %.1fs", model_id, time.perf_counter() - started)
                self.bus.emit("model_switch", model=model_id, status="done")
            finally:
                self._set_state(IDLE)
                self._broadcast(self._ready_payload())
                self._broadcast(self._models_payload())

    # -- first run -------------------------------------------------------

    def _choose_first_model(self) -> None:
        """Replace the stock default with the model recommended for this machine.

        The default is tuned for a CUDA GPU. On a laptop without one it would
        mean a 1.6 GB download followed by dictation too slow to use, so the
        first launch picks what the Models panel would recommend instead.
        """
        model_id, reason = models.recommend(self.system, self.cfg.asr.language)
        self.cfg.asr = dataclasses.replace(self.cfg.asr, model=model_id, **self._placement())
        self._save_config()
        log.info("first run: chose %s on %s - %s", model_id, self.cfg.asr.device, reason)

    def _ensure_gpu_runtime(self, attempts: int = 3) -> bool:
        """Make sure the NVIDIA libraries are present, downloading them if not.

        Installed copies fetch them on first launch instead of carrying them
        (see cuda_runtime.py); a source checkout already has them in its
        virtualenv. Returns whether the GPU can be used.
        """
        if cuda_runtime.available():
            return True

        def progress(done: int, total: int) -> None:
            self.bus.emit("gpu_runtime", status="downloading", done_bytes=done, total_bytes=total)

        for attempt in range(1, attempts + 1):
            self.bus.emit(
                "gpu_runtime", status="downloading", done_bytes=0, total_bytes=cuda_runtime.TOTAL_BYTES
            )
            try:
                cuda_runtime.install(progress)
            except Exception as exc:
                log.warning("GPU library download failed (attempt %d of %d): %s", attempt, attempts, exc)
                self.bus.emit("gpu_runtime", status="error", message=str(exc))
                if attempt < attempts:
                    time.sleep(5 * attempt)
                continue
            self.bus.emit("gpu_runtime", status="done")
            return cuda_runtime.available()
        return False

    def _run_on_cpu_this_session(self) -> None:
        """Fall back to the CPU when the GPU libraries could not be fetched.

        Not saved, so the next launch tries the download again. The model steps
        down too: a GPU-sized model on the CPU would be too slow to use.
        """
        self.system = dataclasses.replace(self.system, cuda=False, cuda_compute_types=[])
        if self.cfg.asr.device == "cuda":
            model_id, _ = models.recommend(self.system, self.cfg.asr.language)
            self.cfg.asr = dataclasses.replace(
                self.cfg.asr, model=model_id, device="cpu", compute_type="int8"
            )
        log.warning("running on the CPU with %s for this session", self.cfg.asr.model)
        self.bus.emit(
            "error",
            message="Could not download the GPU libraries, so dictation runs on the CPU until the next launch.",
        )
        self._broadcast(self._models_payload())

    def _ensure_model_on_disk(self, attempts: int = 3) -> None:
        """Download the configured model, with progress, before loading it.

        faster-whisper would otherwise download it silently inside load(),
        leaving the app showing LOADING for minutes with no explanation.
        """
        model_id = models.canonical(self.cfg.asr.model)
        if models.spec_for(model_id) is None or models.is_downloaded(model_id):
            return

        for attempt in range(1, attempts + 1):
            finished = threading.Event()
            outcome: list[Exception | None] = []

            def progress(done: int, total: int | None) -> None:
                self.bus.emit(
                    "model_download", model=model_id, status="downloading",
                    done_bytes=done, total_bytes=total,
                )

            def done(error: Exception | None) -> None:
                outcome.append(error)
                finished.set()

            self.bus.emit(
                "model_download", model=model_id, status="downloading", done_bytes=0, total_bytes=None
            )
            if not self._downloader.start(model_id, progress, done):
                raise RuntimeError("another download is already running")
            finished.wait()

            if outcome[0] is None:
                self.bus.emit("model_download", model=model_id, status="done")
                self._broadcast(self._models_payload())
                return

            log.warning("download of %s failed (attempt %d of %d): %s",
                        model_id, attempt, attempts, outcome[0])
            self.bus.emit("model_download", model=model_id, status="error", message=str(outcome[0]))
            if attempt < attempts:
                time.sleep(5 * attempt)

        raise RuntimeError(f"could not download {model_id}: {outcome[0]}")

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.bus.start()
        self._set_state(LOADING)

        self.recorder.open()
        try:
            self.system = system.detect()
        except Exception:
            log.exception("hardware detection failed; the models panel will be empty")
        self._broadcast(self._models_payload())

        if self._first_run and self.system is not None:
            self._choose_first_model()
        if self.system is not None and self.system.cuda and not self._ensure_gpu_runtime():
            self._run_on_cpu_this_session()
        self._ensure_model_on_disk()

        self.asr = build_engine(self.cfg.asr)
        self.asr.load()
        self.hook.start()

        self._ready = True
        self._set_state(IDLE)
        log.info("Prompt Maxxer ready - hold %s to dictate", self.cfg.hotkey.label)
        self._broadcast(self._ready_payload())
        self._broadcast(self._models_payload())

    def run_forever(self) -> None:
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass

    def stop(self) -> None:
        self._stop.set()
        self._cancel_capture_timer()
        self.streaming.stop()
        self.hook.stop()
        self.recorder.close()
        self.asr.unload()
