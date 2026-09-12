"""Drive a dictation cycle and capture the overlay in each state.

Two deliberate choices:

* A focused EDIT window acts as the injection sink, so any recognised text
  lands there instead of in whatever the user happens to have open.
* Screenshots are taken in-process. Shelling out to PowerShell cost about a
  second each, which stretched a 1.3 s simulated key hold into 5.4 s of live
  microphone.
"""

import ctypes
import os
import sys
import time
from ctypes import wintypes as wt

from PIL import ImageGrab

import prompt_maxxer  # noqa: F401
from prompt_maxxer.winapi import INPUT, INPUT_KEYBOARD, KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, user32

VK_RCONTROL = 0xA3
OUT = sys.argv[1] if len(sys.argv) > 1 else "."

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
ES_MULTILINE = 0x0004

user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]

# Capture only the strip of screen the pill occupies: enough to see it in
# context, without putting the whole desktop in a screenshot.
screen = ImageGrab.grab()
SW, SH = screen.size
CROP = (SW // 2 - 420, SH - 300, SW // 2 + 420, SH - 40)


def shoot(name: str) -> None:
    ImageGrab.grab().crop(CROP).save(os.path.join(OUT, f"{name}.png"))


def key(flags: int) -> None:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = VK_RCONTROL
    inp.ki.dwFlags = flags | KEYEVENTF_EXTENDEDKEY
    inp.ki.dwExtraInfo = 0
    user32.SendInput(1, (INPUT * 1)(inp), ctypes.sizeof(INPUT))


sink = user32.CreateWindowExW(
    0, "EDIT", "Prompt Maxxer demo sink",
    WS_OVERLAPPEDWINDOW | WS_VISIBLE | ES_MULTILINE,
    40, 40, 500, 160, None, None, None, None,
)
user32.SetForegroundWindow(sink)
user32.SetFocus(sink)
time.sleep(0.4)

print("holding push-to-talk for ~1.1s...")
key(0)
time.sleep(0.45)
shoot("01-listening")
time.sleep(0.5)
shoot("02-listening-peak")
key(KEYEVENTF_KEYUP)
time.sleep(0.10)
shoot("03-transcribing")
time.sleep(1.1)
shoot("04-result")
time.sleep(1.6)
shoot("05-idle")

user32.DestroyWindow(sink)
print("done")
