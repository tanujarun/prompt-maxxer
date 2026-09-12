"""End-to-end smoke test of the engine loop, with no human in it.

Starts the real pipeline, connects a WebSocket client the way the shell does,
then synthesises a Right Ctrl press/release with SendInput. The hook ignores
events carrying our own injection signature, so a plain SendInput event with a
zero dwExtraInfo is indistinguishable from a real keypress and exercises the
whole path: hook -> capture -> recognise -> emit.

Audio is whatever the microphone hears, so the transcript is expected to be
empty. What is under test is the state machine and the wiring.
"""

import asyncio
import ctypes
import json
import threading
import time

import websockets

import prompt_maxxer  # noqa: F401
from prompt_maxxer.config import Config
from prompt_maxxer.pipeline import Pipeline
from prompt_maxxer.winapi import INPUT, INPUT_KEYBOARD, KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, user32

VK_RCONTROL = 0xA3

seen: list[dict] = []


def tap_hotkey(hold_s: float) -> None:
    """Synthesise a real Right Ctrl press and release."""
    def event(flags: int) -> INPUT:
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = VK_RCONTROL
        inp.ki.wScan = 0
        inp.ki.dwFlags = flags | KEYEVENTF_EXTENDEDKEY
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0  # not our signature: the hook must react
        return inp

    down = (INPUT * 1)(event(0))
    user32.SendInput(1, down, ctypes.sizeof(INPUT))
    time.sleep(hold_s)
    up = (INPUT * 1)(event(KEYEVENTF_KEYUP))
    user32.SendInput(1, up, ctypes.sizeof(INPUT))


async def listen(stop: asyncio.Event) -> None:
    async with websockets.connect("ws://127.0.0.1:8765") as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            event = json.loads(raw)
            if event.get("event") != "level":  # too chatty to log
                seen.append(event)
                print(f"  <- {event}")


def main() -> int:
    cfg = Config.load()
    # Keep the model out of the way of what is being tested here.
    print(f"hotkey: {cfg.hotkey.label}  engine: {cfg.asr.model} on {cfg.asr.device}")

    pipeline = Pipeline(cfg)
    print("starting pipeline (loads the model)...")
    t0 = time.perf_counter()
    pipeline.start()
    print(f"ready in {time.perf_counter() - t0:.1f}s\n")

    stop = asyncio.Event()
    loop = asyncio.new_event_loop()

    def run_client() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(listen(stop))

    client = threading.Thread(target=run_client, daemon=True)
    client.start()
    time.sleep(1.0)  # let the socket connect

    print("simulating a 1.5s push-to-talk press...")
    tap_hotkey(1.5)
    time.sleep(3.0)

    loop.call_soon_threadsafe(stop.set)
    pipeline.stop()

    states = [e["state"] for e in seen if e.get("event") == "state"]
    print(f"\nstates observed: {states}")

    checks = [
        ("hook fired, capture began", "recording" in states),
        ("capture ended, ASR ran", "transcribing" in states),
        ("returned to idle", states and states[-1] == "idle"),
        ("shell received events", len(seen) > 0),
    ]
    print()
    failed = 0
    for label, ok in checks:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
