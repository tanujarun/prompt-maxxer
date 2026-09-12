"""Global push-to-talk detection and hotkey capture, via a WH_KEYBOARD_LL hook.

Two constraints shape this module:

1. Windows silently unhooks a low-level keyboard hook whose callback exceeds
   LowLevelHooksTimeout (300 ms by default). The callback therefore only
   enqueues; every observer runs on a separate dispatch thread.
2. SetWindowsHookEx binds the hook to the calling thread, and delivery requires
   that same thread to pump messages. So the hook owns a dedicated thread with
   its own GetMessage loop, stopped by posting WM_QUIT to it.

Choosing a new key happens here too, rather than in the settings window. Only
this hook sees left and right modifiers as different keys and sees F13-F24 and
media keys at all, and it can swallow the press, so picking a key never types
into whatever happens to have focus.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
from ctypes import wintypes as wt
from typing import Callable, Optional, Tuple

from .winapi import (
    HOOKPROC,
    INJECTION_SIGNATURE,
    KBDLLHOOKSTRUCT,
    VK_ESCAPE,
    VK_LCONTROL,
    WH_KEYBOARD_LL,
    WM_KEYDOWN,
    WM_KEYUP,
    WM_QUIT,
    WM_SYSKEYDOWN,
    WM_SYSKEYUP,
    kernel32,
    user32,
)

log = logging.getLogger(__name__)

PRESS = "press"
RELEASE = "release"
CAPTURED = "captured"

# AltGr is delivered as a synthetic Left Ctrl, marked by this bit in its scan
# code, immediately followed by Right Alt.
_ALTGR_FAKE_CTRL = 0x200

#: (vk_code, scan_code, flags) of a captured key, or None if capture was cancelled.
Captured = Optional[Tuple[int, int, int]]


class PushToTalkHook:
    def __init__(
        self,
        vk_code: int,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_capture: Callable[[Captured], None] | None = None,
        suppress: bool = True,
    ) -> None:
        self._vk = vk_code
        self._on_press = on_press
        self._on_release = on_release
        self._on_capture = on_capture
        self._suppress = suppress

        # True between key-down and key-up. Guards against auto-repeat, which
        # delivers a stream of WM_KEYDOWN while the key is held.
        self._down = False

        self._capturing = False
        # A key whose next key-up must be swallowed: the release of a key picked
        # in capture mode, whose press the focused app never saw.
        self._swallow_up: int | None = None

        self._events: "queue.Queue[tuple | None]" = queue.Queue()
        self._hook = None
        self._thread_id = 0
        self._hook_thread: threading.Thread | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._ready = threading.Event()
        # ctypes does not keep the trampoline alive for us; if this were
        # garbage collected while the hook is installed, Windows would call
        # into freed memory.
        self._proc = HOOKPROC(self._callback)

    # -- control (any thread) --------------------------------------------

    @property
    def vk_code(self) -> int:
        return self._vk

    def set_key(self, vk_code: int) -> None:
        """Rebind push-to-talk. Takes effect on the next key event."""
        # Clear the held flag first, so a key that was down when the binding
        # changed cannot leave a recording running with no release to end it.
        self._down = False
        self._vk = vk_code

    def begin_capture(self) -> None:
        """Treat the next key press as the new push-to-talk key."""
        self._capturing = True

    def cancel_capture(self) -> None:
        if self._capturing:
            self._capturing = False
            self._events.put((CAPTURED, None))

    # -- hook callback (hot path, must return fast) ----------------------

    def _callback(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code != 0:
            return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

        kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents

        # Ignore our own injected keystrokes echoing back through the hook.
        if kb.dwExtraInfo == INJECTION_SIGNATURE:
            return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

        vk = kb.vkCode
        is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
        is_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

        if is_up and vk == self._swallow_up:
            self._swallow_up = None
            return 1

        if self._capturing and is_down:
            # Skip AltGr's synthetic Left Ctrl, so pressing AltGr captures
            # Right Alt rather than a Left Ctrl the user never pressed.
            if vk == VK_LCONTROL and kb.scanCode & _ALTGR_FAKE_CTRL:
                return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)
            self._capturing = False
            self._swallow_up = vk
            captured = None if vk == VK_ESCAPE else (vk, kb.scanCode, kb.flags)
            self._events.put((CAPTURED, captured))
            return 1

        if vk == self._vk:
            if is_down:
                if not self._down:
                    self._down = True
                    self._events.put((PRESS,))
                if self._suppress:
                    return 1
            elif is_up:
                if self._down:
                    self._down = False
                    self._events.put((RELEASE,))
                if self._suppress:
                    return 1

        return user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    # -- threads ---------------------------------------------------------

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                return
            kind = event[0]
            try:
                if kind == PRESS:
                    self._on_press()
                elif kind == RELEASE:
                    self._on_release()
                elif kind == CAPTURED and self._on_capture is not None:
                    self._on_capture(event[1])
            except Exception:
                log.exception("hotkey observer failed on %s", kind)

    def _hook_loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        self._hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._ready.set()
            log.error("SetWindowsHookExW failed: %s", ctypes.get_last_error())
            return
        self._ready.set()
        log.info("keyboard hook installed (vk=0x%02X)", self._vk)

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        log.info("keyboard hook removed")

    def start(self) -> None:
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, name="prompt-maxxer-hotkey-dispatch", daemon=True
        )
        self._dispatch_thread.start()
        self._hook_thread = threading.Thread(
            target=self._hook_loop, name="prompt-maxxer-hotkey-hook", daemon=True
        )
        self._hook_thread.start()
        self._ready.wait(timeout=5)
        if not self._hook:
            raise RuntimeError(
                "Could not install the keyboard hook. Note that Prompt Maxxer cannot "
                "see keystrokes sent to an elevated window unless Prompt Maxxer is "
                "also running elevated."
            )

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._events.put(None)
        for t in (self._hook_thread, self._dispatch_thread):
            if t is not None:
                t.join(timeout=2)
