"""Verify live partials against a known WAV, with no microphone involved.

Feeds a real recording into the recorder in real time from a fake audio
thread, so the streaming worker sees a buffer growing exactly as it would
during a live utterance.
"""

import sys
import threading
import time
import wave

import numpy as np
import soxr

import prompt_maxxer  # noqa: F401
from prompt_maxxer.config import Config
from prompt_maxxer.streaming import split_stable


def load_wav(path, rate=16000):
    with wave.open(path, "rb") as w:
        sr, ch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if sr != rate:
        a = soxr.resample(a, sr, rate)
    return a.astype(np.float32)


# --- unit: the stable/tentative split -------------------------------------

CASES = [
    ("", "what if", ("", "what if")),
    ("what if I have", "what if I have a lot", ("what if I have", "a lot")),
    ("what if I have a", "what if I had a", ("what if I", "had a")),
    ("hello world", "hello world", ("hello world", "")),
    # The case that caused visible flicker: hyphenation churn must not
    # invalidate the whole sentence.
    (
        "Sato is a push to talk dictation app",
        "Sato is a push-to-talk dictation app. Hold the",
        ("Sato is a push-to-talk dictation app.", "Hold the"),
    ),
    ("a b c", "a b c d", ("a b c", "d")),
]
print("split_stable:")
unit_fail = 0
for prev, cur, want in CASES:
    got = split_stable(prev, cur)
    ok = got == want
    unit_fail += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {prev!r} -> {cur!r} = {got!r}")

# --- integration: partials over a growing buffer --------------------------

audio = load_wav(sys.argv[1])
print(f"\nclip: {len(audio) / 16000:.2f}s")

cfg = Config.load()
cfg.ipc.port = 8799  # do not collide with a running engine
from prompt_maxxer.pipeline import Pipeline  # noqa: E402

pipeline = Pipeline(cfg)
print("loading model...")
pipeline.bus.start()
pipeline.recorder.open()
pipeline.asr.load()

partials = []
pipeline.bus.emit = lambda event, **kw: (
    partials.append(kw) if event == "partial" else None
)

# Replay the clip into the recorder as if the microphone produced it.
pipeline.recorder._capturing = True
pipeline.recorder._chunks = []
pipeline.recorder._started_at = time.monotonic()
pipeline.recorder._resample_needed = False


def feed():
    block = 320  # 20 ms
    for i in range(0, len(audio), block):
        with pipeline.recorder._lock:
            pipeline.recorder._chunks.append(audio[i : i + block])
        time.sleep(block / 16000)


print("streaming...\n")
started = time.perf_counter()
feeder = threading.Thread(target=feed, daemon=True)
feeder.start()
pipeline.streaming.start()
feeder.join()
time.sleep(0.6)
pipeline.streaming.stop()
time.sleep(0.5)

for p in partials:
    at = p["audio_s"]
    print(f"  t={at:5.2f}s ({p['latency_ms']:3d} ms)  {p['stable']} | {p['tentative']}")

print(f"\n{len(partials)} partials over {len(audio) / 16000:.1f}s of audio")
ok = len(partials) >= 3 and any(p["stable"] for p in partials)
print(f"[{'PASS' if ok else 'FAIL'}] partials produced with stabilised prefixes")

pipeline.recorder.close()
raise SystemExit(0 if ok and not unit_fail else 1)
