"""System tray icon (pystray) — mic glyph; menu: record, log window, live mode, quit."""
import pystray
from PIL import Image, ImageDraw


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
                pystray.MenuItem("Recordings log…", lambda: self.c.request("show_log")),
                pystray.MenuItem("Live typing mode",
                                 lambda: self.c.request("toggle_live"),
                                 checked=lambda item: self.c.config.get("mode") == "live"),
                pystray.MenuItem("Quit", lambda: self.c.request("quit")),
            ),
        )

    def start(self):
        self.icon.run_detached()

    def set_recording(self, recording):
        self.icon.icon = _icon_image(recording)
        self.icon.title = "Dictate — recording…" if recording else "Dictate — ready"

    def stop(self):
        try:
            self.icon.stop()
        except Exception:
            pass
