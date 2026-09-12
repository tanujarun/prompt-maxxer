"""Find where the fixed per-utterance cost actually goes."""

import sys
import time
import wave

import numpy as np
import soxr

import prompt_maxxer  # registers CUDA DLL directories
from faster_whisper import WhisperModel


def load_wav(path, target_rate=16000):
    with wave.open(path, "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != target_rate:
        audio = soxr.resample(audio, rate, target_rate)
    return audio.astype(np.float32)


def timeit(model, audio, reps=5, **kwargs):
    for _ in range(2):  # warm
        list(model.transcribe(audio, **kwargs)[0])
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        segs, _info = model.transcribe(audio, **kwargs)
        text = "".join(s.text for s in segs)
        times.append((time.perf_counter() - t0) * 1000)
    return float(np.median(times)), text.strip()


def main():
    full = load_wav(sys.argv[1])
    short = full[: 16000 * 3]
    print(f"clips: 3.00s and {len(full) / 16000:.2f}s\n")

    base = dict(beam_size=1, condition_on_previous_text=False, without_timestamps=True)

    for compute_type in ("int8_float16", "float16"):
        print(f"=== compute_type={compute_type} ===")
        model = WhisperModel("large-v3-turbo", device="cuda", compute_type=compute_type)
        list(model.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])

        variants = [
            ("autodetect lang + VAD", dict(base, vad_filter=True)),
            ("lang=en      + VAD", dict(base, vad_filter=True, language="en")),
            ("lang=en      no VAD", dict(base, vad_filter=False, language="en")),
        ]
        for label, kwargs in variants:
            ms3, _ = timeit(model, short, **kwargs)
            msf, text = timeit(model, full, **kwargs)
            print(f"  {label:24s}  3s: {ms3:6.1f} ms   full: {msf:6.1f} ms")
        print(f"  sample: {text[:70]}")
        del model
        print()


if __name__ == "__main__":
    main()
