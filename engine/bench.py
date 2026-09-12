"""Load the model and measure real end-to-end recognition latency."""

import sys
import time
import wave

import numpy as np
import soxr

import prompt_maxxer  # registers CUDA DLL directories
from prompt_maxxer.asr import build_engine
from prompt_maxxer.config import AsrConfig


def load_wav(path: str, target_rate: int = 16000) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != target_rate:
        audio = soxr.resample(audio, rate, target_rate)
    return audio.astype(np.float32)


def main() -> int:
    path = sys.argv[1]
    audio = load_wav(path)
    print(f"audio: {len(audio) / 16000:.2f}s @ 16 kHz")

    cfg = AsrConfig()
    print(f"engine: {cfg.model} / {cfg.device} / {cfg.compute_type}")

    engine = build_engine(cfg)
    t0 = time.perf_counter()
    engine.load()
    print(f"load + warmup: {time.perf_counter() - t0:.1f}s")

    for i in range(3):
        result = engine.transcribe(audio, 16000)
        print(
            f"run {i + 1}: {result.latency_s * 1000:7.1f} ms  "
            f"({result.realtime_factor:5.1f}x realtime)  lang={result.language}"
        )
    print(f"\ntext: {result.text}")

    # Latency for a realistic short utterance, which is what dictation is.
    short = audio[: 16000 * 3]
    t0 = time.perf_counter()
    r = engine.transcribe(short, 16000)
    print(f"\n3.0s utterance -> {(time.perf_counter() - t0) * 1000:.0f} ms")
    print(f"text: {r.text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
