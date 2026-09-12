"""Text injection into whatever window currently has focus.

Two strategies, because neither is good at both jobs:

* SendInput with KEYEVENTF_UNICODE synthesises real keystrokes. It works in
  essentially every window, needs no clipboard, and fires input events that
  editors and web inputs react to correctly - but it costs roughly a
  millisecond per character, so a long paragraph visibly types itself out.
* Clipboard + Ctrl+V is O(1) regardless of length, but it clobbers whatever
  the user had copied, so we save and restore it.

Short text takes the first path, long text the second.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes as wt

from .winapi import (
    INPUT,
    INPUT_KEYBOARD,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    INJECTION_SIGNATURE,
    VK_CONTROL,
    VK_RETURN,
    VK_V,
    kernel32,
    user32,
)

log = logging.getLogger(__name__)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wt.HGLOBAL
kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
kernel32.GlobalLock.restype = wt.LPVOID
kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
kernel32.GlobalUnlock.restype = wt.BOOL
user32.OpenClipboard.argtypes = [wt.HWND]
user32.OpenClipboard.restype = wt.BOOL
user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
user32.SetClipboardData.restype = wt.HANDLE
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetClipboardData.restype = wt.HANDLE


def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = scan
    inp.ki.dwFlags = flags
    inp.ki.time = 0
    inp.ki.dwExtraInfo = INJECTION_SIGNATURE
    return inp


def _send(inputs: list[INPUT]) -> None:
    if not inputs:
        return
    arr = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise ctypes.WinError(ctypes.get_last_error())


def type_unicode(text: str, delay_ms: int = 0) -> None:
    """Type text as synthetic Unicode keystrokes."""
    batch: list[INPUT] = []
    for ch in text:
        if ch == "\r":
            continue
        if ch == "\n":
            # KEYEVENTF_UNICODE with U+000A does not produce a newline in most
            # controls; the Return virtual key does.
            batch.append(_key_input(VK_RETURN, 0, 0))
            batch.append(_key_input(VK_RETURN, 0, KEYEVENTF_KEYUP))
            continue
        # Characters outside the BMP need their UTF-16 surrogate pair sent as
        # two separate events.
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = units[i] | (units[i + 1] << 8)
            batch.append(_key_input(0, code, KEYEVENTF_UNICODE))
            batch.append(_key_input(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))

    if delay_ms <= 0:
        _send(batch)
        return

    for i in range(0, len(batch), 2):
        _send(batch[i : i + 2])
        time.sleep(delay_ms / 1000)


def _open_clipboard(retries: int = 10) -> bool:
    """Another process may own the clipboard; back off and retry briefly."""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def get_clipboard_text() -> str | None:
    if not _open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.c_wchar_p(ptr).value
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, buf, size)
        kernel32.GlobalUnlock(handle)
        # On success the system takes ownership of the handle, so it must not
        # be freed here.
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


def paste_via_clipboard(text: str, restore: bool = True, restore_delay_ms: int = 450) -> None:
    previous = get_clipboard_text() if restore else None

    if not set_clipboard_text(text):
        log.warning("clipboard unavailable; typing instead")
        type_unicode(text)
        return

    _send(
        [
            _key_input(VK_CONTROL, 0, 0),
            _key_input(VK_V, 0, 0),
            _key_input(VK_V, 0, KEYEVENTF_KEYUP),
            _key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP),
        ]
    )

    if previous is None:
        return

    # Ctrl+V only queues the paste; the target reads the clipboard whenever it
    # gets round to processing its message queue. Restoring inline raced that
    # and the app pasted the *old* contents. So restore on a timer, and off
    # the calling thread - the dictation pipeline must not block on this.
    timer = threading.Timer(restore_delay_ms / 1000, set_clipboard_text, args=(previous,))
    timer.daemon = True
    timer.start()


def inject(text: str, cfg) -> str:
    """Insert text at the caret. Returns the strategy used."""
    if not text:
        return "noop"
    if len(text) <= cfg.clipboard_threshold:
        type_unicode(text, cfg.keystroke_delay_ms)
        return "keystrokes"
    paste_via_clipboard(text, cfg.restore_clipboard, cfg.restore_delay_ms)
    return "clipboard"
