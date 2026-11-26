"""Deliver text to the user: clipboard + Ctrl+V paste, or live SendInput typing.

Only keyboard.send/write are used from the `keyboard` lib (pure SendInput, no
global hook). Key-state checks go through GetAsyncKeyState — installing the
lib's hook listener breaks the Windows key.
"""
import ctypes
import os
import re
import time

import keyboard

VK_LWIN, VK_RWIN, VK_CONTROL, VK_SPACE = 0x5B, 0x5C, 0x11, 0x20


def _keys_held():
    gak = ctypes.windll.user32.GetAsyncKeyState
    return any(gak(vk) & 0x8000 for vk in (VK_LWIN, VK_RWIN, VK_CONTROL, VK_SPACE))


def _wait_hotkey_release(timeout=1.5):
    """Don't paste while the toggle keys are still held (Win+V etc. surprises)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _keys_held():
            return
        time.sleep(0.03)


def set_clipboard(root, text):
    """Uses the tk root (main thread only)."""
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update()  # flush so the clipboard survives even if we exit


def paste(root, text):
    set_clipboard(root, text)
    _wait_hotkey_release()
    time.sleep(0.05)
    keyboard.send("ctrl+v")


def type_text(text):
    """Live-typing mode: unicode SendInput, works for Hebrew."""
    _wait_hotkey_release()
    keyboard.write(text, delay=0)


# ---- live-typing target window ("hold and resume") -------------------------
# SendInput goes to whatever is foreground, so live mode remembers the window
# a take started in and only types while that window is foreground again.
# Checks WINDOWS, not caret position: another text box in the same window
# still receives the text (accepted limit).
_u32 = ctypes.windll.user32
_u32.GetForegroundWindow.restype = ctypes.c_void_p
_u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_ulong)]
_u32.IsWindow.argtypes = [ctypes.c_void_p]
_MY_PID = os.getpid()


def foreground_window():
    """Foreground HWND as an int, or None. Windows of THIS process (the pill,
    the log window, the tray menu) count as None — never a typing target."""
    hwnd = _u32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_ulong()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return None if pid.value == _MY_PID else hwnd


def window_exists(hwnd):
    return bool(hwnd) and bool(_u32.IsWindow(hwnd))


_CMD = re.compile(
    r"(?P<pre>[,.!?;:])?\s*\bnew ?(?P<kind>line|paragraph)\b(?P<post>[,.!?;:])?\s*",
    re.IGNORECASE)


def apply_voice_commands(text):
    """Spoken 'new line' / 'new paragraph' become actual breaks — but only when
    the phrase is set off by punctuation (a slight pause while dictating), so
    sentences ABOUT newlines ('I was hoping for a new line ability') survive."""
    def repl(m):
        if not (m.group("pre") or m.group("post")):
            return m.group(0)
        br = "\n\n" if m.group("kind").lower() == "paragraph" else "\n"
        pre = m.group("pre") or ""
        return (pre if pre in ".!?" else "") + br
    return _CMD.sub(repl, text)


# Conservative list — "er"/"mm"/"hmm" excluded, they appear deliberately.
_FILLER = re.compile(r"(?:,\s*)?\b(?:um+|uh+m*|erm+)\b,?", re.IGNORECASE)
_RECAP = re.compile(r"(^|[.!?]\s+)([a-z])")


def remove_fillers(text):
    """Drop verbal fillers by plain phrase match (no LLM), then repair the
    punctuation/capitalization the removal leaves behind."""
    text = _FILLER.sub(" ", text)
    text = re.sub(r"  +", " ", text)
    text = re.sub(r"\s+([.!?,])", r"\1", text)       # space before punctuation
    text = re.sub(r"([.!?])[.]+", r"\1", text)       # doubled periods
    text = re.sub(r",([.!?])", r"\1", text)          # dangling comma
    text = re.sub(r"^\s*[.,!?]\s*", "", text).strip()  # leading orphans
    return _RECAP.sub(lambda m: m.group(1) + m.group(2).upper(), text)
