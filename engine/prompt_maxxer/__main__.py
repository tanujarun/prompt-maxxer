"""Engine entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .audio import list_input_devices
from .config import Config, config_dir
from .pipeline import Pipeline

_instance_mutex = None


def _claim_single_instance() -> bool:
    """One engine per session. A second would install a second keyboard hook,
    and both engines would record and type every take."""
    global _instance_mutex
    import ctypes

    from .winapi import ERROR_ALREADY_EXISTS, kernel32

    _instance_mutex = kernel32.CreateMutexW(None, False, "PromptMaxxerEngine")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _configure_logging(verbose: bool) -> Path:
    """Log to stderr and to a rotating file.

    The file matters most on other people's machines: the engine runs hidden
    behind the app, so when something goes wrong there, the log is the only
    record of what happened.
    """
    log_dir = config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "engine.log"

    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # -v is for debugging Prompt Maxxer, not for watching huggingface_hub
    # negotiate HTTP/1.1. These drown the log at DEBUG and hide what matters.
    for noisy in ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file


def _transcribe_file(path: str, cfg: Config) -> int:
    """Diagnostic: transcribe a WAV with the configured model, print it, exit.

    Exercises the whole recognition stack - CUDA libraries, model load, a real
    forward pass - without a microphone, a hotkey or the app, so it can check
    an installed build on any machine.
    """
    import time
    import wave

    import numpy as np

    from .asr import build_engine

    with wave.open(path, "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        print("expected a 16-bit PCM WAV file")
        return 2

    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        import soxr

        audio = soxr.resample(audio, rate, 16000).astype(np.float32)

    engine = build_engine(cfg.asr)
    started = time.perf_counter()
    engine.load()
    loaded = time.perf_counter() - started
    result = engine.transcribe(audio, 16000)

    print(f"model: {cfg.asr.model} on {engine.device_in_use} ({cfg.asr.compute_type}), loaded in {loaded:.1f}s")
    print(f"audio: {result.audio_s:.1f}s transcribed in {result.latency_s * 1000:.0f} ms")
    print(f"text:  {result.text}")
    return 0 if result.text.strip() else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="prompt-maxxer-engine", description="Prompt Maxxer dictation engine")
    parser.add_argument("--list-devices", action="store_true", help="list input devices and exit")
    parser.add_argument("--transcribe", metavar="WAV", help="transcribe a WAV file with the configured model and exit")
    parser.add_argument("--device", help="input device index or name substring")
    parser.add_argument("--model", help="override the ASR model")
    parser.add_argument("--cpu", action="store_true", help="force CPU inference")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.list_devices:
        for dev in list_input_devices():
            print(f"[{dev['index']:>2}] {dev['name']}  ({dev['default_samplerate']:.0f} Hz)")
        return 0

    log_file = _configure_logging(args.verbose)
    log = logging.getLogger(__name__)

    if args.transcribe:
        # Read the config without creating one, so a diagnostic run on a fresh
        # machine does not use up its first-run setup.
        existing = config_dir() / "config.json"
        cfg = Config.load() if existing.exists() else Config()
    else:
        if not _claim_single_instance():
            log.error("the engine is already running; exiting")
            return 0
        cfg = Config.load()

    if args.device is not None:
        cfg.audio.device = int(args.device) if args.device.isdigit() else args.device
    if args.model:
        cfg.asr.model = args.model
    if args.cpu:
        cfg.asr.device = "cpu"
        cfg.asr.compute_type = "int8"

    if args.transcribe:
        if cfg.asr.device == "cuda":
            from . import cuda_runtime

            if not cuda_runtime.available():
                print("GPU libraries are not installed (the app downloads them on first launch); "
                      "checking on the CPU instead.")
                cfg.asr.device = "cpu"
                cfg.asr.compute_type = "int8"
        return _transcribe_file(args.transcribe, cfg)

    frozen = getattr(sys, "frozen", False)
    log.info("config: %s | log: %s | %s", config_dir() / "config.json", log_file,
             "installed build" if frozen else "source checkout")

    # On the first launch, pick the model for this machine - unless one was
    # asked for explicitly on the command line.
    first_run = cfg.first_run and not args.model and not args.cpu
    pipeline = Pipeline(cfg, first_run=first_run)
    try:
        pipeline.start()
        pipeline.run_forever()
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("engine stopped")
        return 1
    finally:
        pipeline.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
