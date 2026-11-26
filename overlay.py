"""Floating always-on-top recording pill: red dot, live waveform, timer, X to cancel.

Click the pill body = done (stop + paste). Click the small X = cancel (audio still
saved + logged). Colors: dark pill; dot red while streaming, amber if the stream
is down and we're recording locally only. All geometry is DPI-scaled.
"""
import math
import time
import tkinter as tk

BASE_W, BASE_H = 320, 56
WARN_EXTRA_H = 34  # extra height for the silence-warning band
BARS = 23
BG = "#1e1f24"
FG = "#e8e8ea"
RED = "#e5484d"
AMBER = "#f5a524"
BAR = "#4cc38a"
BAR_DIM = "#2a5c44"
LANG = "#7aa2f7"   # language badge (Hebrew-only take)


class OverlayPill:
    def __init__(self, root, on_done, on_cancel, on_toggle_mode=None, on_wake=None):
        self.root = root
        # DPI scale: 1.0 at 96 dpi, 1.5 at 150% scaling
        self.s = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.w = int(BASE_W * self.s)
        self.h = int(BASE_H * self.s)
        self.warn_h = self.h + int(WARN_EXTRA_H * self.s)
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.on_toggle_mode = on_toggle_mode
        self.on_wake = on_wake
        self.win = None
        self.canvas = None
        self.stream_ok = True
        self.live_mode = False
        self.lang_badge = None   # e.g. "HE" for a Hebrew-only take, None = normal
        self.started_at = None
        self.silence_remaining = None  # secs until auto-stop, or None = no warning
        self.held = 0      # live turns held because the target window isn't foreground
        self._note_token = 0  # bumps on every show(); a stale note timer won't hide a new take
        self._blink = False

    def _ensure_win(self):
        if self.win is None:
            self.win = tk.Toplevel(self.root)
            self.win.overrideredirect(True)
            self.win.attributes("-topmost", True)
            self.win.attributes("-alpha", 0.94)
            self.canvas = tk.Canvas(self.win, width=self.w, height=self.h, bg=BG,
                                    highlightthickness=0, cursor="hand2")
            self.canvas.pack()
            self.canvas.bind("<Button-1>", self._click)
        self.canvas.config(height=self.h)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        y = sh - self.h - int(80 * self.s)
        self.win.geometry(f"{self.w}x{self.h}+{(sw - self.w) // 2}+{y}")
        self.win.deiconify()
        self.win.lift()

    def show(self, live_mode=False, lang_badge=None):
        self.live_mode = live_mode
        self.lang_badge = lang_badge
        self.stream_ok = True
        self.silence_remaining = None
        self.held = 0
        self.started_at = time.time()
        self._note_token += 1
        self._ensure_win()

    def set_held(self, n):
        self.held = n

    def show_note(self, text, secs=5):
        """After a take: a static amber note (no recording row), auto-hides.
        Any click dismisses it. Used when live text was held, not typed."""
        self._note_token += 1
        token = self._note_token
        self.started_at = None
        self._ensure_win()
        c, s = self.canvas, self.s
        c.delete("all")
        r = 7 * s
        c.create_oval(14 * s, self.h / 2 - r, 14 * s + 2 * r, self.h / 2 + r,
                      fill=AMBER, outline="")
        c.create_text(40 * s, self.h / 2, text=text, fill=FG, anchor="w",
                      width=self.w - 54 * s, font=("Segoe UI", 9))
        self.root.after(int(secs * 1000),
                        lambda: self._note_token == token and self.hide())

    def hide(self):
        if self.win is not None:
            self.win.withdraw()

    def set_stream_ok(self, ok):
        self.stream_ok = ok

    def set_silence(self, remaining):
        """remaining = secs until auto-stop (shows the warning band), None = clear."""
        was_on = self.silence_remaining is not None
        self.silence_remaining = remaining
        now_on = remaining is not None
        if was_on != now_on and self.win is not None:
            new_h = self.warn_h if now_on else self.h
            self.canvas.config(height=new_h)
            # grow downward into the bottom margin; top edge stays put
            self.win.geometry(f"{self.w}x{new_h}+{self.win.winfo_x()}+{self.win.winfo_y()}")

    def _click(self, event):
        if self.started_at is None:  # post-take note showing: click = dismiss
            self._note_token += 1
            self.hide()
            return
        # hit zones: mode badge bottom-left, X at right edge, rest = done
        if self.silence_remaining is not None:
            # warning active: ✓ and ✕ keep their meaning, anything else = "I'm here"
            if event.x >= self.w - int(34 * self.s) and event.y < self.h:
                self.on_cancel()
            elif event.x >= self.w - int(66 * self.s) and event.y < self.h:
                self.on_done()
            elif self.on_wake:
                self.on_wake()
            return
        if (self.on_toggle_mode and event.x < 150 * self.s
                and event.y > self.h - 18 * self.s):
            self.on_toggle_mode()
        elif event.x >= self.w - int(34 * self.s):
            self.on_cancel()
        else:
            self.on_done()

    def draw(self, levels):
        """Called ~20x/sec by the controller while recording."""
        if self.win is None or self.win.state() == "withdrawn":
            return
        s, w, h = self.s, self.w, self.h
        c = self.canvas
        c.delete("all")

        # status dot — solid, calm (no blinking)
        r = 7 * s
        c.create_oval(14 * s, h / 2 - r, 14 * s + 2 * r, h / 2 + r,
                      fill=RED if self.stream_ok else AMBER, outline="")

        # waveform: log-scaled against a FIXED reference so quiet rooms don't
        # get renormalized into jumpy full-height noise
        vals = list(levels)[-BARS:]
        vals = [0] * (BARS - len(vals)) + vals
        x0 = 44 * s
        bar_w = 5 * s
        gap = 2 * s
        max_h = h - 22 * s
        color = BAR if self.stream_ok else AMBER
        for i, v in enumerate(vals):
            frac = math.log10(1 + v / 60) / math.log10(1 + 32768 / 60)
            bh = max(2 * s, frac * max_h)
            x = x0 + i * (bar_w + gap)
            c.create_rectangle(x, h / 2 - bh / 2, x + bar_w, h / 2 + bh / 2,
                               fill=color if v > 120 else BAR_DIM, width=0)

        # timer
        secs = int(time.time() - self.started_at) if self.started_at else 0
        c.create_text(w - 66 * s, h / 2, text=f"{secs // 60}:{secs % 60:02d}",
                      fill=FG, font=("Segoe UI", 11, "bold"), anchor="e")

        # language badge — a one-shot monolingual take must SAY so while it runs
        x_mode = 14 * s
        if self.lang_badge:
            item = c.create_text(14 * s, h - 8 * s, text=f"{self.lang_badge} ONLY",
                                 fill=LANG, font=("Segoe UI", 7, "bold"), anchor="w")
            x_mode = c.bbox(item)[2] + 8 * s

        # mode badge (clickable — toggles live typing vs paste at end)
        if self.live_mode:
            c.create_text(x_mode, h - 8 * s, text="⚡ LIVE TYPING", fill=BAR,
                          font=("Segoe UI", 7, "bold"), anchor="w")
        else:
            c.create_text(x_mode, h - 8 * s, text="⏎ PASTE AT END", fill="#8b8d98",
                          font=("Segoe UI", 7, "bold"), anchor="w")
        note = []
        if not self.stream_ok:
            note.append("offline — recording locally" if not self.held else "offline")
        if self.held:  # calm, static: target window isn't foreground
            note.append(f"{self.held} held — go back to resume")
        if note:
            c.create_text(w - 40 * s, h - 8 * s, text=" · ".join(note),
                          fill=AMBER, font=("Segoe UI", 7), anchor="e")

        # silence-warning band below the normal row
        if self.silence_remaining is not None:
            rem = max(0, int(self.silence_remaining))
            c.create_text(w / 2, h + 17 * s,
                          text=f"⚠ no audio — auto-stop in {rem // 60}:{rem % 60:02d}"
                               " · click or speak",
                          fill=AMBER, font=("Segoe UI", 9, "bold"))

        # done ✓ (green) — same action as clicking anywhere on the pill body
        c.create_line(w - 58 * s, h / 2, w - 52 * s, h / 2 + 6 * s,
                      fill=BAR, width=3, capstyle="round")
        c.create_line(w - 52 * s, h / 2 + 6 * s, w - 42 * s, h / 2 - 6 * s,
                      fill=BAR, width=3, capstyle="round")
        # cancel X
        c.create_line(w - 26 * s, h / 2 - 6 * s, w - 14 * s, h / 2 + 6 * s,
                      fill="#8b8d98", width=2)
        c.create_line(w - 26 * s, h / 2 + 6 * s, w - 14 * s, h / 2 - 6 * s,
                      fill="#8b8d98", width=2)
