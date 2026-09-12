"""Drive the overlay through a full dictation with scripted events.

Occupies port 8765 so the shell attaches to this instead of starting the real
engine. Replays partials captured from an actual run, so the UI is exercised
with realistic churn - including the tail being revised - and screenshots the
pill at each interesting moment.
"""

import asyncio
import json
import math
import sys

import websockets
from PIL import ImageGrab

OUT = sys.argv[1] if len(sys.argv) > 1 else "."

# (audio_s, stable, tentative) taken verbatim from a real 12.8 s utterance.
PARTIALS = [
    (1.26, "", "StarCode is a push to..."),
    (1.78, "", "Prompt Maxxer is a push to talk to T-"),
    (2.30, "Prompt Maxxer is a push to talk", "dictation app."),
    (3.33, "Prompt Maxxer is a push to talk dictation app.", "Hold the right control."),
    (4.39, "Prompt Maxxer is a push to talk dictation app. Hold the right control key.", ""),
    (4.92, "Prompt Maxxer is a push-to-talk dictation app. Hold the right control key.", "Speak naturally."),
    (6.51, "Prompt Maxxer is a push-to-talk dictation app. Hold the right control key. Speak naturally. And the", "transcribe."),
    (7.59, "Prompt Maxxer is a push to talk dictation app, hold the right control key, speak naturally, and the transcribed text appears", "wherever."),
    (8.12, "Prompt Maxxer is a push to talk dictation app. Hold the right control key. Speak naturally. And the transcribed text appears wherever", "you're cursing."),
    (8.66, "Prompt Maxxer is a push to talk dictation app. Hold the right control key. Speak naturally. And the transcribed text appears wherever", "your cursor is."),
    (10.84, "Prompt Maxxer is a push to talk dictation app. Hold the right control key. Speak naturally. And the transcribed text appears wherever your cursor is. It runs entirely", "on you."),
    (11.95, "Prompt Maxxer is a push to talk dictation app. Hold the right control key. Speak naturally. And the transcribed text appears wherever your cursor is. It runs entirely on your local", "graphics card."),
]

FINAL = (
    "Prompt Maxxer is a push-to-talk dictation app. Hold the right control key, speak "
    "naturally, and the transcribed text appears wherever your cursor is. It "
    "runs entirely on your local graphics card."
)

shot_index = 0


def shoot(name: str) -> None:
    """Crop to the strip of screen the pill occupies."""
    img = ImageGrab.grab()
    w, h = img.size
    img.crop((w // 2 - 640, h - 320, w // 2 + 640, h - 60)).save(f"{OUT}/{name}.png")
    print(f"  captured {name}")


async def drive(ws) -> None:
    async def send(event: str, **kw):
        await ws.send(json.dumps({"event": event, **kw}))

    await send("ready", hotkey="Right Ctrl", engine="faster-whisper", streaming=True)
    await send("state", state="idle")
    await asyncio.sleep(1.0)

    print("recording...")
    await send("state", state="recording")

    tick = 0
    for index, (audio_s, stable, tentative) in enumerate(PARTIALS):
        # Feed level frames between partials so the meter animates.
        while tick * 0.05 < audio_s:
            rms = 0.05 + 0.05 * abs(math.sin(tick * 0.55)) + 0.02 * math.sin(tick * 2.3)
            await send("level", rms=rms)
            await asyncio.sleep(0.05)
            tick += 1
        await send("partial", stable=stable, tentative=tentative,
                   audio_s=audio_s, latency_ms=110)
        if index == 2:
            await asyncio.sleep(0.4)
            shoot("live-01-early")
        if index == 6:
            await asyncio.sleep(0.4)
            shoot("live-02-midway")
        if index == len(PARTIALS) - 1:
            await asyncio.sleep(0.4)
            shoot("live-03-long")

    print("transcribing...")
    await send("state", state="transcribing", audio_s=12.8)
    await asyncio.sleep(0.5)
    shoot("live-04-transcribing")

    await send("transcript", text=FINAL, language="en", audio_s=12.8,
               asr_ms=191, total_ms=214, strategy="clipboard")
    await asyncio.sleep(0.6)
    shoot("live-05-final")
    await asyncio.sleep(1.6)
    shoot("live-06-idle")
    print("done")


async def handler(ws):
    print("shell connected")
    try:
        await drive(ws)
    except Exception as exc:
        print("driver error:", exc)


async def main():
    async with websockets.serve(handler, "127.0.0.1", 8765):
        print("fake engine on ws://127.0.0.1:8765 - launch the shell now")
        await asyncio.sleep(70)


asyncio.run(main())
