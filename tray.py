"""System tray icon (pystray) — mic glyph; menu: record, Hebrew-only take, log
window, live mode, live hold, quit. Right-click IS the menu (pystray); left-click runs the
default item, so a Hebrew take is a menu entry, not its own mouse binding."""
import pystray
from PIL import Image, ImageDraw

_KEY_NAMES = {"windows": "Win", "win": "Win", "ctrl": "Ctrl", "control": "Ctrl",
              "shift": "Shift", "alt": "Alt"}


def _pretty_hotkey(hk):
    return "+".join(_KEY_NAMES.get(t, t.capitalize())
                    for t in hk.lower().replace(" ", "").split("+"))


def _icon_image(recording=False):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (229, 72, 77, 255) if recording else (76, 195, 138, 255)
    d.rounded_rectangle([24, 8, 40, 38], radius=8, fill=color)      # capsule
    d.arc([16, 22, 48, 50], start=0, end=180, fill=color, width=4)  # cradle
    d.line([32, 50, 32, 58], fill=color, width=4)                   # stem
    d.line([22, 58, 42, 58], fill=color, width=4)                   # base
    return img


class Tray:
    def __init__(self, controller):
        self.c = controller
        self.icon = pystray.Icon(
            "dictate", _icon_image(), "Dictate — ready",
            menu=pystray.Menu(
                pystray.MenuItem("Start/stop recording", lambda: self.c.request("toggle"),
                                 default=True),
                # Right-click entry point for a one-shot Hebrew-only take; the
                # hotkey is spelled out here so it never has to be remembered.
                pystray.MenuItem(
                    lambda item: "Hebrew only — one take  ("
                                 + _pretty_hotkey(self.c.config.get(
                                     "hebrew_hotkey", "windows+ctrl+shift+space")) + ")",
                    lambda: self.c.request("toggle_he")),
                pystray.MenuItem("Recordings log…", lambda: self.c.request("show_log")),
                pystray.MenuItem("Live typing mode",
                                 lambda: self.c.request("toggle_live"),
                                 checked=lambda item: self.c.config.get("mode") == "live"),
                # Live typing only goes into the window the take started in;
                # in another window the text waits and resumes when you're back.
                pystray.MenuItem("Live: hold text while I'm in another window",
                                 lambda: self.c.request("toggle_hold"),
                                 checked=lambda item: self.c.hold_enabled()),
                pystray.MenuItem("Quit", lambda: self.c.request("quit")),
            ),
        )

    def start(self):
        self.icon.run_detached()

    def set_recording(self, recording, langs=None):
        self.icon.icon = _icon_image(recording)
        if not recording:
            self.icon.title = "Dictate — ready"
        elif langs:
            self.icon.title = "Dictate — recording… (%s only)" % "+".join(langs)
        else:
            self.icon.title = "Dictate — recording…"

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass
