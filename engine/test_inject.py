"""Verify SendInput injection against a real Win32 EDIT control.

An EDIT control is the right target: it is the actual Win32 text control that
most native apps embed, and unlike Tk (whose Tcl 8.6 core is UCS-2 and cannot
even hold a non-BMP character) it round-trips the full Unicode range, so a
failure here is a real failure in the injection code.
"""

import ctypes
import time
from ctypes import wintypes as wt

import prompt_maxxer  # noqa: F401
from prompt_maxxer.config import InjectConfig
from prompt_maxxer.inject import get_clipboard_text, inject, set_clipboard_text, type_unicode

user32 = ctypes.WinDLL("user32", use_last_error=True)

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_VISIBLE = 0x10000000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
ES_AUTOVSCROLL = 0x0040
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
PM_REMOVE = 0x0001

user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPVOID]


def pump(seconds: float) -> None:
    """Run the message loop so the control actually receives injected input."""
    msg = wt.MSG()
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.005)


def read_text(hwnd) -> str:
    length = user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, None) + 1
    buf = ctypes.create_unicode_buffer(length)
    user32.SendMessageW(hwnd, WM_GETTEXT, length, buf)
    return buf.value.replace("\r\n", "\n")


def clear(hwnd) -> None:
    user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.create_unicode_buffer(""))


hwnd = user32.CreateWindowExW(
    0, "EDIT", "Prompt Maxxer injection test",
    WS_OVERLAPPEDWINDOW | WS_VISIBLE | WS_VSCROLL | ES_MULTILINE | ES_AUTOVSCROLL,
    200, 200, 620, 300, None, None, None, None,
)
if not hwnd:
    raise ctypes.WinError(ctypes.get_last_error())

user32.SetForegroundWindow(hwnd)
user32.SetFocus(hwnd)
pump(0.4)

CASES = [
    ("ascii", "Hello from Prompt Maxxer."),
    ("punctuation", "It costs $15/mo - about 3.5x cheaper, right?"),
    ("accents", "Café français, naïve, Zürich"),
    ("cyrillic", "Привет, мир"),
    ("cjk", "你好世界"),
    ("non-bmp", "done \U0001F642 ok"),
    ("newline", "line one\nline two"),
]

results = []
for name, payload in CASES:
    clear(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.SetFocus(hwnd)
    pump(0.15)
    type_unicode(payload)
    pump(0.35)
    got = read_text(hwnd)
    results.append((name, payload, got, got == payload))

# Clipboard path, including that the previous clipboard is restored afterwards.
sentinel = "PROMPT-MAXXER-PREVIOUS-CLIPBOARD"
set_clipboard_text(sentinel)
long_text = "The quick brown fox jumps over the lazy dog. " * 5
clear(hwnd)
user32.SetForegroundWindow(hwnd)
user32.SetFocus(hwnd)
pump(0.15)
strategy = inject(long_text, InjectConfig())
pump(0.4)
got = read_text(hwnd)
results.append(("clipboard-paste", long_text, got, got == long_text))
results.append(("clipboard-strategy", "clipboard", strategy, strategy == "clipboard"))
pump(0.8)  # let the restore timer fire
restored = get_clipboard_text()
results.append(("clipboard-restore", sentinel, restored or "", restored == sentinel))

user32.DestroyWindow(hwnd)

print()
failed = 0
for name, want, got, ok in results:
    if not ok:
        failed += 1
    shown = got if len(got) < 52 else got[:49] + "..."
    print(f"[{'PASS' if ok else 'FAIL'}] {name:20s} {shown!r}")
    if not ok:
        print(f"       expected {want[:49]!r}")

print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
