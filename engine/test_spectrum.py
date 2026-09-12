"""Check the visualizer's band analysis on known signals and on real speech.

Feeds signals through BandAnalyzer exactly as the audio callback does - a
block at a time, reading bands about thirty times a second - and checks that
silence and room tone stay flat, that a low tone lights the low bands and a
high tone the high bands, and that speech moves the display. It also prints the
speech as dot-matrix frames, so the look can be judged, not just the numbers.
"""

import sys
import wave

import numpy as np
import soxr

from prompt_maxxer.spectrum import BANDS, BandAnalyzer

RATE = 16000
BLOCK = RATE // 50  # 20 ms, like the capture stream
ROWS = 7

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def frames(signal: np.ndarray) -> np.ndarray:
    analyzer = BandAnalyzer(RATE)
    out = []
    for i, start in enumerate(range(0, len(signal), BLOCK)):
        analyzer.feed(signal[start : start + BLOCK].astype(np.float32))
        if i % 2 == 0:  # ~25-30 reads a second
            out.append(analyzer.bands())
    return np.array(out)


def dots(value: float) -> int:
    return max(1, round(value * ROWS)) if value > 0.02 else 0


def render(frame) -> list[str]:
    heights = [dots(v) for v in frame]
    return ["".join("█" if ROWS - row <= h else "·" for h in heights) for row in range(ROWS)]


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return soxr.resample(audio, rate, RATE).astype(np.float32) if rate != RATE else audio


t = np.arange(RATE) / RATE
rng = np.random.default_rng(1)

silence = frames(np.zeros(RATE))
check("silence is flat", silence.max() == 0)

room = frames(rng.standard_normal(RATE) * 0.0015)  # about -56 dBFS
check("quiet room tone stays flat", room.max() == 0, f"max {room.max():.2f}")

low = frames(0.1 * np.sin(2 * np.pi * 150 * t))[-1]
check("a 150 Hz tone lights the low bands", int(np.argmax(low)) <= 3, f"tallest band {int(np.argmax(low))}")

high = frames(0.1 * np.sin(2 * np.pi * 3000 * t))[-1]
check("a 3 kHz tone lights the high bands", int(np.argmax(high)) >= 10, f"tallest band {int(np.argmax(high))}")

if len(sys.argv) > 1:
    # Scale the synthetic voice to a typical microphone level (about -26 dBFS
    # RMS while talking) rather than trusting the synthesiser's own level.
    speech = load_wav(sys.argv[1])
    voiced = speech[np.abs(speech) > 0.01]
    speech = speech * (10 ** (-26 / 20) / float(np.sqrt(np.mean(voiced**2))))
    f = frames(speech)
    talking = f[f.max(axis=1) > 0]
    tallest = np.array([max(dots(v) for v in frame) for frame in talking])
    lit_columns = np.array([sum(dots(v) > 0 for v in frame) for frame in talking])
    per_band = talking.mean(axis=0)
    moving = np.abs(np.diff(talking, axis=0)).mean()

    print(f"\n   speech frames: {len(f)}, with sound: {len(talking)}")
    print(f"   tallest column (dots): median {np.median(tallest):.0f}, 90th pct {np.percentile(tallest, 90):.0f}")
    print(f"   lit columns per frame: median {np.median(lit_columns):.0f} of {BANDS}")
    print("   mean height by band:  " + " ".join(f"{v:.2f}" for v in per_band))
    check("speech reaches most of the height", np.percentile(tallest, 90) >= 5)
    check("speech does not light every column at once", np.median(lit_columns) < BANDS)
    check("speech moves frame to frame", moving > 0.03, f"mean change {moving:.3f}")

    picks = np.linspace(len(f) * 0.15, len(f) * 0.85, 4).astype(int)
    print("\n   sample frames:")
    blocks = [render(f[i]) for i in picks]
    for row in range(ROWS):
        print("   " + "   ".join(b[row] for b in blocks))

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
