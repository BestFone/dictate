"""Log/recovery window: every recording, with Copy / Retry / Play / Open folder."""
import os
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import inject
import logstore
import retry


class LogWindow:
    def __init__(self, root, api_key, config, controller=None):
        self.root = root
        self.api_key = api_key
        self.config = config
        self.controller = controller
        self.win = None
        self.tree = None
        self.status_var = None
        self.live_var = None

    def show(self):
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self.refresh()
        self.win.deiconify()
        self.win.lift()

    def _build(self):
        self.win = tk.Toplevel(self.root)
        self.win.title("Dictate — recordings")
        self.win.geometry("860x420")
        self.win.protocol("WM_DELETE_WINDOW", self.win.withdraw)

        bar = ttk.Frame(self.win)
        bar.pack(fill="x", padx=8, pady=6)
        ttk.Button(bar, text="Copy transcript", command=self.copy_sel).pack(side="left")
        ttk.Button(bar, text="Retry transcription", command=self.retry_sel).pack(side="left", padx=6)
        ttk.Button(bar, text="Play audio", command=self.play_sel).pack(side="left")
        ttk.Button(bar, text="Open folder", command=lambda: os.startfile(logstore.REC_DIR)).pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        self.live_var = tk.BooleanVar(value=self.config.get("mode") == "live")
        ttk.Checkbutton(bar, text="Live typing mode", variable=self.live_var,
                        command=self._toggle_live).pack(side="right")
        self.fillers_var = tk.BooleanVar(value=self.config.get("remove_fillers", True))
        ttk.Checkbutton(bar, text="Remove fillers (um, uh)", variable=self.fillers_var,
                        command=self._toggle_fillers).pack(side="right", padx=8)

        style = ttk.Style(self.win)
        style.configure("Dictate.Treeview", rowheight=28)

        cols = ("time", "dur", "status", "words", "wpm", "transcript")
        self.tree = ttk.Treeview(self.win, columns=cols, show="headings",
                                 style="Dictate.Treeview")
        headings = {"time": "Time", "dur": "Dur", "status": "Status",
                    "words": "Words", "wpm": "WPM", "transcript": "Transcript"}
        widths = {"time": 115, "dur": 45, "status": 90, "words": 50, "wpm": 45,
                  "transcript": 480}
        for c in cols:
            anchor = "e" if c in ("dur", "words", "wpm") else "w"
            self.tree.heading(c, text=headings[c])
            self.tree.column(c, width=widths[c], anchor=anchor,
                             stretch=(c == "transcript"))
        self.tree.pack(fill="both", expand=True, padx=8)
        self.tree.tag_configure("failed", foreground="#c62828")
        self.tree.tag_configure("cancelled", foreground="#a06a00")

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.win, textvariable=self.status_var, anchor="w").pack(fill="x", padx=8, pady=4)

    def _toggle_fillers(self):
        self.config["remove_fillers"] = self.fillers_var.get()
        if self.controller:
            self.controller.save()

    def _toggle_live(self):
        mode = "live" if self.live_var.get() else "paste"
        if self.controller:
            self.controller.set_mode(mode)
        else:
            self.config["mode"] = mode

    def refresh(self):
        if not self.tree:
            return
        self.tree.delete(*self.tree.get_children())
        entries = logstore.read_all()
        total_sec = sum(e.get("duration", 0) for e in entries)
        for e in reversed(entries):
            status = e.get("status", "?")
            tags = ("failed",) if status in ("stream_failed", "retry_failed",
                                             "recovered") else \
                   (("cancelled",) if status == "cancelled" else ())
            preview = (e.get("transcript") or "").replace("\n", " ")
            words = len(preview.split())
            dur = e.get("duration", 0)
            wpm = f"{words / (dur / 60):.0f}" if words and dur >= 2 else ""
            # id "2026-07-27_005630" -> "07-27 00:56"
            rid = e.get("id", "")
            when = f"{rid[5:10]} {rid[11:13]}:{rid[13:15]}" if len(rid) >= 15 else rid
            self.tree.insert("", "end", iid=e["id"], tags=tags, values=(
                when, f"{dur:.0f}s", status, words or "", wpm, preview))
        cost = total_sec / 3600 * 0.45
        self.status_var.set(f"{len(entries)} recordings · {total_sec/60:.1f} min total · ~${cost:.2f} streamed")

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Dictate", "Select a recording first.", parent=self.win)
            return None
        eid = sel[0]
        for e in logstore.read_all():
            if e["id"] == eid:
                return e
        return None

    def copy_sel(self):
        e = self._selected()
        if not e:
            return
        text = e.get("transcript") or ""
        if not text:
            messagebox.showinfo("Dictate", "No transcript on this entry — use Retry.", parent=self.win)
            return
        inject.set_clipboard(self.root, text)
        self.status_var.set("Transcript copied to clipboard.")

    def play_sel(self):
        e = self._selected()
        if e and e.get("wav") and os.path.exists(e["wav"]):
            os.startfile(e["wav"])

    def retry_sel(self):
        e = self._selected()
        if not e:
            return
        if not (e.get("wav") and os.path.exists(e["wav"])):
            messagebox.showerror("Dictate", "Audio file is missing for this entry.", parent=self.win)
            return
        self.status_var.set(f"Retrying {e['id']} via async API…")
        threading.Thread(target=self._retry_worker, args=(e,), daemon=True).start()

    def _retry_worker(self, e):
        try:
            text = retry.transcribe_file(e["wav"], self.api_key,
                                         self.config.get("language_codes"))
            logstore.update(e["id"], transcript=text, status="ok_retried")
            self.root.after(0, lambda: (self.refresh(),
                                        inject.set_clipboard(self.root, text),
                                        self.status_var.set("Retry done — transcript copied to clipboard.")))
        except Exception as err:
            logstore.update(e["id"], status="retry_failed", error=str(err))
            self.root.after(0, lambda: (self.refresh(),
                                        self.status_var.set(f"Retry failed: {err}")))
