"""Dictate — push-to-talk dictation on AssemblyAI Universal-Streaming.

Win+Ctrl+Space toggles recording (configurable in config.json). Esc or the
pill's X cancels — audio is ALWAYS saved and logged, cancel included.
Run with pythonw for no console window.
"""
import collections
import datetime
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk

import audio
import gui
import hotkey
import inject
import logstore
import overlay
import retry
import stream
import tray

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
KEY_PATH = os.path.join(os.path.expanduser("~"), ".config", "assemblyai", "api_key")
SINGLETON_PORT = 51733

# Contextual prompt for a Hebrew-only take — DELIBERATELY EMPTY. Measured on
# two saved Hebrew WAVs (A/B/C/D through the real socket): `language_codes:
# ["he"]` alone returned everything in Hebrew script, an ENGLISH-language prompt
# describing the audio as Hebrew put English back INTO the output, and the same
# prompt written in Hebrew changed nothing. AssemblyAI's own guidance is to
# start with no prompt. Set "hebrew_prompt" in config.json to experiment.
HEBREW_PROMPT = ""

logging.basicConfig(
    filename=os.path.join(APP_DIR, "app.log"),
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("dictate")


def load_config():
    if not os.path.exists(CONFIG_PATH):  # first run: bootstrap from the example
        import shutil
        shutil.copyfile(os.path.join(APP_DIR, "config.example.json"), CONFIG_PATH)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_api_key(config):
    key = (config.get("api_key") or "").strip()
    if not key:
        key = os.environ.get("ASSEMBLYAI_API_KEY", "").strip()
    if not key:
        try:
            with open(KEY_PATH, encoding="utf-8") as f:
                key = f.read().strip()
        except OSError:
            pass
    if not key:
        root = tk.Tk()
        root.withdraw()
        from tkinter import messagebox
        messagebox.showerror(
            "Dictate — no API key",
            "No AssemblyAI API key found.\n\n"
            "Get one at https://www.assemblyai.com/ then either:\n"
            "  • put it in config.json as \"api_key\", or\n"
            "  • set the ASSEMBLYAI_API_KEY environment variable, or\n"
            f"  • save it as the only contents of:\n     {KEY_PATH}",
        )
        sys.exit(1)
    return key


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


class App:
    def __init__(self):
        self.config = load_config()
        self.api_key = load_api_key(self.config)

        recovered = logstore.recover_orphans()
        if recovered:
            log.warning("recovered %d orphaned recording(s) from a previous crash",
                        recovered)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Dictate")

        self.commands = queue.Queue()  # thread-safe bridge into the tk main loop
        self.recording = False
        self.recorder = None
        self.session = None
        self.rec_id = None
        self.take_langs = None   # per-take language override (Hebrew-only take)
        self._ended_turns = {}   # turn_order -> final text
        self._typed_turns = set()
        self._type_lock = threading.Lock()
        self._finalize_done = threading.Event()
        self._finalize_done.set()

        self.pill = overlay.OverlayPill(self.root,
                                        on_done=lambda: self.request("stop"),
                                        on_cancel=lambda: self.request("cancel"),
                                        on_toggle_mode=lambda: self.request("toggle_live"),
                                        on_wake=lambda: self.request("wake"))
        self.logwin = gui.LogWindow(self.root, self.api_key, self.config, controller=self)
        self.tray = tray.Tray(self)

        self._register_hotkey()
        self.tray.start()
        self.root.after(30, self._poll)
        if recovered:
            # Surface the salvage immediately — recovery must be visible.
            self.root.after(800, self.logwin.show)
        log.info("started; hotkey=%s mode=%s", self.config["hotkey"], self.config["mode"])

    # ---- thread-safe command bridge -------------------------------------
    def request(self, cmd):
        self.commands.put(cmd)

    def _poll(self):
        try:
            while True:
                cmd = self.commands.get_nowait()
                if cmd == "toggle":
                    self.stop_recording() if self.recording else self.start_recording()
                elif cmd == "toggle_he":
                    # One-shot Hebrew-only take; the next normal press is en+he
                    # again, so there is no mode left switched on by accident.
                    (self.stop_recording() if self.recording
                     else self.start_recording(langs=self.hebrew_langs(),
                                               prompt=self.hebrew_prompt()))
                elif cmd == "stop" and self.recording:
                    self.stop_recording()
                elif cmd == "cancel" and self.recording:
                    self.stop_recording(cancelled=True)
                elif cmd == "show_log":
                    self.logwin.show()
                elif cmd == "toggle_live":
                    self.set_mode("live" if self.config.get("mode") != "live" else "paste")
                elif cmd == "wake" and self.recording:
                    # "I'm still here" click on the silence warning
                    self._last_voice = time.time()
                    self.pill.set_silence(None)
                elif cmd == "quit":
                    return self.quit()
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    # ---- hotkeys ----------------------------------------------------------
    def hebrew_langs(self):
        return self.config.get("hebrew_language_codes") or ["he"]

    def hebrew_prompt(self):
        return self.config.get("hebrew_prompt", HEBREW_PROMPT)

    def _register_hotkey(self):
        self.hotkeys = hotkey.HotkeyThread(
            self.config.get("hotkey", "windows+ctrl+space"),
            on_main=lambda: self.request("toggle"),
            on_esc=lambda: self.request("cancel"),
            esc_active=lambda: self.recording,
            extra=[(self.config.get("hebrew_hotkey", "windows+ctrl+shift+space"),
                    lambda: self.request("toggle_he"))],
        )
        self.hotkeys.start()
        self.hotkeys.wait_ready()

    # ---- recording lifecycle -----------------------------------------------
    def start_recording(self, langs=None, prompt=None):
        """langs/prompt override the config for THIS take only (one-shot).
        A single code (e.g. ["he"]) makes it a monolingual session; the prompt
        additionally DESCRIBES the audio as Hebrew, which is the second (and
        weaker) documented steer — together they stop the model code-switching
        back into English/transliteration."""
        self.rec_id = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.take_langs = langs
        self._ended_turns = {}
        self._typed_turns = set()
        self.recorder = audio.Recorder(sample_rate=self.config.get("sample_rate", 16000),
                                       device=self.config.get("input_device"))
        # Log the row up front: if we die mid-take, the log SHOWS a row stuck
        # in 'recording' and startup salvage turns it into 'recovered'.
        logstore.append({
            "id": self.rec_id,
            "wav": os.path.join(logstore.REC_DIR, f"{self.rec_id}.wav"),
            "transcript": "", "status": "recording", "duration": 0,
            "model": self.config.get("speech_model"), "error": None,
            "langs": langs or self.config.get("language_codes") or [],
        })
        try:
            self.recorder.start(self.rec_id)
        except Exception as e:
            log.error("mic failed to start: %s", e)
            logstore.update(self.rec_id, status="mic_failed", error=str(e))
            self.pill.show()
            self.pill.set_stream_ok(False)
            self.root.after(1500, self.pill.hide)
            return
        take_config = self.config
        if langs or prompt:
            take_config = dict(self.config)
            if langs:
                take_config["language_codes"] = langs
            if prompt:
                take_config["prompt"] = prompt
        self.session = stream.StreamSession(
            self.api_key, self.recorder.send_queue, take_config,
            on_turn=self._on_turn,
            on_status=lambda s: self.root.after(0, self._on_stream_status, s),
        )
        self.session.start()
        self.recording = True
        self._last_voice = time.time()
        self._floor_hist = collections.deque(maxlen=600)  # 30 s of chunk RMS
        self.pill.show(live_mode=self.config.get("mode") == "live",
                       lang_badge=" ".join(c.upper() for c in langs) if langs else None)
        self.tray.set_recording(True, langs=langs)
        self.hotkeys.grab_esc()
        self._tick()
        log.info("recording %s started (langs=%s)", self.rec_id,
                 langs or self.config.get("language_codes"))

    def _on_stream_status(self, status):
        # Pill dot goes amber while a dropped stream reconnects, back on success.
        if status in ("failed", "reconnecting"):
            self.pill.set_stream_ok(False)
        elif status == "connected" and self.recording:
            self.pill.set_stream_ok(True)

    def _on_turn(self, msg):
        """ws thread. Record every ended turn; in live mode type it right away.
        Gate on end_of_turn only — waiting for turn_is_formatted can skip turns
        whose formatted message never arrives (observed with short phrases)."""
        if not self.recording:
            return
        if msg.get("end_of_turn"):
            order = msg.get("turn_order", 0)
            text = (msg.get("transcript") or "").strip()
            if text:
                self._ended_turns[order] = text
            if self.config.get("mode") == "live":
                self._type_pending()

    def _process(self, text):
        """Delivery-side text transforms; the log always keeps the raw text."""
        text = inject.apply_voice_commands(text)
        if self.config.get("remove_fillers", True):
            text = inject.remove_fillers(text)
        return text

    def _type_pending(self):
        """Type every ended-but-untyped turn, in order. Called from the ws
        thread on new turns AND from the main thread when switching to live
        mid-recording (catch-up on everything said before the switch)."""
        with self._type_lock:
            for order in sorted(self._ended_turns):
                if order in self._typed_turns:
                    continue
                self._typed_turns.add(order)
                text = self._process(self._ended_turns[order])
                if not text:
                    continue  # turn was pure filler
                inject.type_text(text if text.endswith("\n") else text + " ")

    def _tick(self):
        if not self.recording:
            return
        self.pill.draw(self.recorder.levels)
        self._check_silence()
        self.root.after(50, self._tick)  # matches the 50 ms audio chunk cadence

    def _check_silence(self):
        """Auto-stop a forgotten-open mic. Fixed RMS cutoffs don't work here: the
        webcam mic's AGC keeps a silent room at RMS ~290, so 'voice' is adaptive —
        well above the rolling noise floor, sustained for 3 chunks (a mouse click
        is 1-2 chunks and must not reset the timer). Validated offline against
        real silent + speech WAVs: 0 false resets / 0 false warnings."""
        timeout = self.config.get("silence_autostop_secs", 150)
        if not timeout:
            return
        recent = list(self.recorder.levels)[-3:]
        if recent:
            self._floor_hist.append(recent[-1])
            floor = sorted(self._floor_hist)[len(self._floor_hist) // 10]
            thr = max(2.5 * floor, floor + 120)
            if len(recent) == 3 and all(v > thr for v in recent):
                self._last_voice = time.time()
                self.pill.set_silence(None)
                return
        quiet = time.time() - self._last_voice
        warn_at = max(timeout - 60, timeout / 2)
        if quiet >= timeout:
            log.info("silence auto-stop after %.0fs of no audio", quiet)
            self.pill.set_silence(None)
            self.stop_recording(auto=True)
        elif quiet >= warn_at:
            self.pill.set_silence(timeout - quiet)

    def stop_recording(self, cancelled=False, auto=False):
        self.recording = False
        self.hotkeys.release_esc()
        self.tray.set_recording(False)
        self.pill.hide()

        recorder, session, rec_id = self.recorder, self.session, self.rec_id
        recorder.stop()
        duration = recorder.duration()
        wav_path = recorder.wav_path
        if session:
            session.stop()

        # Finalize off the UI thread (waits for the last Turns), then deliver.
        self._finalize_done.clear()
        threading.Thread(target=self._finalize,
                         args=(session, rec_id, wav_path, duration, cancelled, auto),
                         daemon=True).start()

    def _finalize(self, session, rec_id, wav_path, duration, cancelled, auto=False):
        transcript = ""
        failed = True
        if session:
            # After a reconnect the resent backlog goes at real time, so the
            # stream can still be catching up when the take stops.
            backlog = session.backlog_secs()
            behind = bool(session.drops) and backlog > stream.MAX_STOP_WAIT
            if cancelled:
                session.abort()   # audio is saved either way; don't wait on the server
            elif behind:
                # Waiting out a long backlog is slower than the async retry.
                log.info("%.0fs backlog at stop after reconnect; handing to async retry",
                         backlog)
                session.abort()
            session.join(timeout=8 + (0 if behind else backlog))
            transcript = session.transcript()
            # Empty transcript normally means the stream silently died — but a
            # silence auto-stopped take is GENUINELY empty; don't retry those.
            # Still alive = stuck reconnecting: let the async retry recover it.
            failed = session.failed or behind or session.is_alive() or (
                not transcript and duration > 1.0 and not auto)
        status = ("cancelled" if cancelled
                  else "stream_failed" if failed
                  else "ok_reconnected" if session.drops
                  else "ok")
        # Live mode: whatever never got typed (last partial turn, or turns from
        # before a mode switch that stayed in paste mode) is the tail.
        tail = ""
        if session and not cancelled and self.config.get("mode") == "live":
            with self._type_lock:
                parts = [t for o, t in sorted(session.turns.items())
                         if o not in self._typed_turns and t.strip()]
            tail = " ".join(parts)
        logstore.update(rec_id, transcript=transcript, status=status,
                        duration=round(duration, 1),
                        error=getattr(session, "error", None))
        log.info("recording %s finished: %s (%.1fs, %d chars)",
                 rec_id, status, duration, len(transcript))
        self._finalize_done.set()
        self.root.after(0, self._deliver, transcript, status, tail, rec_id, wav_path)

    def _deliver(self, transcript, status, tail="", rec_id=None, wav_path=None):
        """Main thread: clipboard + paste per mode/status."""
        live = self.config.get("mode") == "live"
        if status == "cancelled":
            pass  # never touch the clipboard on cancel
        elif transcript:
            processed = self._process(transcript) or transcript
            if live:
                tail = self._process(tail)
                if tail:  # finish typing what live mode hadn't gotten to yet
                    inject.type_text(tail)
                inject.set_clipboard(self.root, processed)
            elif not self.config.get("autopaste", True):
                inject.set_clipboard(self.root, processed)
            else:
                inject.paste(self.root, processed)
        if status == "stream_failed":
            # Recovery must be visible: surface the log window with the failed row,
            # then recover from the local WAV without waiting for a Retry click.
            self.logwin.show()
            if rec_id and wav_path and os.path.exists(wav_path):
                self.logwin.set_status("Stream dropped — auto-retrying via async API…")
                threading.Thread(target=self._auto_retry_worker,
                                 args=(rec_id, wav_path), daemon=True).start()
        elif self.logwin.win and self.logwin.win.winfo_exists() \
                and self.logwin.win.state() != "withdrawn":
            self.logwin.refresh()

    def _auto_retry_worker(self, rec_id, wav_path):
        """Clipboard-only recovery: focus may have moved by now, so never paste,
        and never re-type in live mode (turns were already typed)."""
        try:
            text = retry.transcribe_file(wav_path, self.api_key,
                                         self.config.get("language_codes"))
        except Exception as err:
            log.error("auto-retry for %s failed: %s", rec_id, err)
            logstore.update(rec_id, status="retry_failed", error=str(err))
            # err=err: Python deletes `err` when the except block ends.
            self.root.after(0, lambda err=err: (self.logwin.refresh(),
                                                self.logwin.set_status(f"Auto-retry failed: {err}")))
            return
        logstore.update(rec_id, transcript=text, status="ok_retried")
        log.info("auto-retry for %s recovered %d chars", rec_id, len(text))
        self.root.after(0, lambda: (self.logwin.refresh(),
                                    inject.set_clipboard(self.root, self._process(text) or text),
                                    self.logwin.set_status("Recovered — transcript copied to clipboard.")))

    # ---- misc ---------------------------------------------------------------
    def save(self):
        save_config(self.config)

    def set_mode(self, mode):
        self.config["mode"] = mode
        save_config(self.config)
        self.pill.live_mode = (mode == "live")
        if self.logwin.live_var is not None:
            self.logwin.live_var.set(mode == "live")
        # Switching to live mid-recording: catch up on everything already said.
        if mode == "live" and self.recording:
            threading.Thread(target=self._type_pending, daemon=True).start()

    def quit(self):
        if self.recording:
            # Graceful: save + log the in-flight take (no paste), then exit.
            self.stop_recording(cancelled=True)
        self.tray.stop()
        self.hotkeys.shutdown()
        self._wait_quit(time.time() + 10)

    def _wait_quit(self, deadline):
        if self._finalize_done.is_set() or time.time() > deadline:
            self.root.destroy()
        else:
            self.root.after(100, self._wait_quit, deadline)

    def run(self):
        self.root.mainloop()


def _serve_guard(sock, app):
    """The singleton socket doubles as a control channel: a later launch with
    --restart sends 'quit' and we shut down gracefully (in-flight recording
    saved + logged) so it can take over."""
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            data = conn.recv(16)
            if b"quit" in data:
                log.info("takeover requested — quitting gracefully")
                app.request("quit")
        finally:
            conn.close()


def _bind_singleton(takeover):
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", SINGLETON_PORT))
        return guard
    except OSError:
        pass
    if not takeover:
        sys.exit(0)
    # Ask the running instance to quit gracefully, then wait for the port.
    try:
        with socket.create_connection(("127.0.0.1", SINGLETON_PORT),
                                      timeout=2) as c:
            c.sendall(b"quit\n")
    except OSError:
        pass  # older build without control channel; it must be killed manually
    for _ in range(150):
        time.sleep(0.1)
        try:
            guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            guard.bind(("127.0.0.1", SINGLETON_PORT))
            return guard
        except OSError:
            continue
    sys.exit(1)


def main():
    # DPI awareness so tkinter geometry maps 1:1 to physical pixels
    # (otherwise the overlay pill lands mid-screen on scaled monitors).
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    guard = _bind_singleton(takeover="--restart" in sys.argv)
    guard.listen(1)
    app = App()
    threading.Thread(target=_serve_guard, args=(guard, app), daemon=True).start()
    app.run()


if __name__ == "__main__":
    main()
