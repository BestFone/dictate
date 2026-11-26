"""Global hotkey via a minimal WH_KEYBOARD_LL hook.

Why not the `keyboard` lib: its suppress engine eats the Windows key wholesale
(breaks Win / Win+R). Why not RegisterHotKey: Windows reserves Win+Ctrl+Space
for IME switching, so registration fails.

This hook suppresses ONLY the trigger key's keydown when the required
modifiers are held (and Esc while recording). Modifier keys themselves are
never touched, so all other Win/Ctrl combos behave normally.
"""
import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger("dictate.hotkey")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_QUIT = 0x0012
VK_ESC = 0x1B

VK = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "capslock": 0x14, "pause": 0x13,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
# modifier name -> VKs, any of which counts as "held"
MOD_VKS = {
    "ctrl": (0x11,), "control": (0x11,),
    "alt": (0x12,),
    "shift": (0x10,),
    "win": (0x5B, 0x5C), "windows": (0x5B, 0x5C),
}

HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int,
                              wintypes.WPARAM, wintypes.LPARAM)

# Explicit prototypes — default int-sized returns truncate 64-bit handles
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                     wintypes.HMODULE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = wintypes.LPARAM
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]


def parse(hotkey_str):
    mods, vk = [], None
    for tok in hotkey_str.lower().replace(" ", "").split("+"):
        if tok in MOD_VKS:
            mods.append(MOD_VKS[tok])
        elif tok in VK:
            vk = VK[tok]
        elif len(tok) == 1:
            vk = user32.VkKeyScanW(ord(tok)) & 0xFF
        else:
            raise ValueError(f"unknown key {tok!r} in hotkey {hotkey_str!r}")
    if vk is None:
        raise ValueError(f"no non-modifier key in hotkey {hotkey_str!r}")
    return mods, vk


class HotkeyThread(threading.Thread):
    """on_main fires on the configured combo; extra is [(hotkey_str, callback)]
    for additional combos (e.g. the Hebrew-only take). on_esc fires on Esc
    whenever esc_active() is true (i.e. while recording). All are suppressed
    from reaching other apps; nothing else is intercepted."""

    def __init__(self, hotkey_str, on_main, on_esc, esc_active=lambda: False,
                 extra=()):
        super().__init__(daemon=True)
        self.on_esc = on_esc
        self.esc_active = esc_active
        combos = [(*parse(hotkey_str), on_main, hotkey_str)]
        for hk, callback in extra:
            if hk:
                combos.append((*parse(hk), callback, hk))
        # Most modifiers first: win+ctrl+SHIFT+space must be matched before
        # win+ctrl+space, whose modifiers are also held when shift is down.
        self.combos = sorted(combos, key=lambda c: -len(c[0]))
        self.hotkey_str = hotkey_str
        self.tid = None
        self._proc = HOOKPROC(self._hook)  # keep a reference or it's GC'd

    @staticmethod
    def _mods_held(mods):
        return all(any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in group)
                   for group in mods)

    def _hook(self, n_code, w_param, l_param):
        if n_code == 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            for mods, vk, callback, _name in self.combos:
                if kb.vkCode == vk and self._mods_held(mods):
                    if MOD_VKS["win"] in mods:
                        # Win+<swallowed key> looks like a lone Win tap, so
                        # Start opens on release. A dummy key in between
                        # (vk 0xE8, unassigned — AutoHotkey's "menu mask")
                        # tells Windows another key was pressed.
                        user32.keybd_event(0xE8, 0, 0, 0)
                        user32.keybd_event(0xE8, 0, 2, 0)  # KEYEVENTF_KEYUP
                    try:
                        callback()
                    except Exception:
                        log.exception("hotkey callback")
                    return 1  # swallow just this key event
            if kb.vkCode == VK_ESC and self.esc_active():
                try:
                    self.on_esc()
                except Exception:
                    log.exception("esc callback")
                return 1
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def run(self):
        self.tid = threading.get_native_id()
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not handle:
            log.error("SetWindowsHookEx failed (err %d)", kernel32.GetLastError())
            return
        log.info("hotkeys hooked: %s", ", ".join(c[3] for c in self.combos))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass  # LL hook is serviced by this loop; no messages expected
        user32.UnhookWindowsHookEx(handle)

    def wait_ready(self, timeout=3):
        return True

    def grab_esc(self):
        pass  # esc gating is dynamic via esc_active()

    def release_esc(self):
        pass

    def shutdown(self):
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
