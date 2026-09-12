"""Naming keys, and deciding which keys may be the push-to-talk key.

The push-to-talk key is swallowed system-wide for as long as the app runs, so a
key that types text or drives editing would stop working everywhere. Those are
refused, with a reason the settings window can show.
"""

from __future__ import annotations

import ctypes

from .winapi import LLKHF_EXTENDED, user32

# Names GetKeyNameText gets wrong or lacks entirely: it returns nothing for
# most media keys, and does not distinguish left from right for some modifiers.
_NAMES: dict[int, str] = {
    0x13: "Pause",
    0x14: "Caps Lock",
    0x2D: "Insert",
    0x5C: "Right Win",
    0x5D: "Menu",
    0x90: "Num Lock",
    0x91: "Scroll Lock",
    0xA1: "Right Shift",
    0xA3: "Right Ctrl",
    0xA5: "Right Alt",
    0xA6: "Browser Back",
    0xA7: "Browser Forward",
    0xA8: "Browser Refresh",
    0xA9: "Browser Stop",
    0xAA: "Browser Search",
    0xAB: "Browser Favorites",
    0xAC: "Browser Home",
    0xAD: "Mute",
    0xAE: "Volume Down",
    0xAF: "Volume Up",
    0xB0: "Next Track",
    0xB1: "Previous Track",
    0xB2: "Stop Media",
    0xB3: "Play/Pause",
    0xB4: "Mail",
    0xB5: "Media Select",
    0xB6: "App 1",
    0xB7: "App 2",
    **{0x70 + i: f"F{i + 1}" for i in range(24)},
}


def key_label(vk: int, scan_code: int = 0, flags: int = 0) -> str:
    """A human name for a key, as the settings window and tray show it."""
    if vk in _NAMES:
        return _NAMES[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk)
    if scan_code:
        lparam = (scan_code & 0xFF) << 16
        if flags & LLKHF_EXTENDED:
            lparam |= 1 << 24
        buf = ctypes.create_unicode_buffer(64)
        if user32.GetKeyNameTextW(lparam, buf, len(buf)) > 0:
            return buf.value
    return f"Key 0x{vk:02X}"


def refusal(vk: int) -> str | None:
    """Why a key cannot be the push-to-talk key, or None if it can."""
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A or 0x60 <= vk <= 0x6F:
        return "it types text"
    if 0xBA <= vk <= 0xC0 or 0xDB <= vk <= 0xDF or vk == 0xE2:
        return "it types text"
    if vk in (0x08, 0x09, 0x0D, 0x20, 0x2E):
        return "you need it for typing"
    if 0x21 <= vk <= 0x28:
        return "you need it to move around text"
    if vk in (0x10, 0x11, 0x12, 0xA0, 0xA2, 0xA4):
        return "the left one drives everyday shortcuts - try the right-hand key"
    if vk == 0x5B:
        return "it opens the Start menu"
    return None
