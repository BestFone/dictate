# Dictate — push-to-talk dictation (AssemblyAI streaming)

Vision + feature spec: `PROJECT-VISION.md`. Single-user Windows tray app, Python + tkinter.

## System map
| Piece | File | Notes |
|---|---|---|
| Entry / controller | `main.py` | tk mainloop owns UI; other threads post commands via `App.request()` queue. DPI awareness set before `tk.Tk()` — don't remove, the pill mispositions without it |
| Mic + WAV | `audio.py` | 16 kHz mono int16; WAV written in the audio callback — recording never depends on the network |
| Streaming | `stream.py` | `wss://streaming.assemblyai.com/v3/ws`, `speech_model=universal-3-5-pro`, `language_codes` repeated in query string (verified accepted). NB: attr is `_stop_evt`, not `_stop` (Thread shadows `_stop`) |
| Hotkey | `hotkey.py` | WH_KEYBOARD_LL hook suppressing ONLY the exact combo + Esc-while-recording. NOT `keyboard.add_hotkey(suppress=True)` (eats the Win key) and NOT RegisterHotKey (Win+Ctrl+Space is OS-reserved, registration fails). Explicit ctypes argtypes required — defaults truncate 64-bit handles (err 126) |
| Overlay pill | `overlay.py` | click body or ✓ = done, ✕/Esc = cancel (cancel STILL saves + logs audio), mode badge bottom-left toggles live/paste. Calm by design: solid dot, log-scaled fixed-reference waveform (see feedback_calm-ambient-ui memory) |
| Delivery | `inject.py` | clipboard+Ctrl+V (waits for hotkey keys released) or live `keyboard.write`. `apply_voice_commands`: spoken "new line"/"new paragraph" → breaks, only when punctuation-set-off (pause), so sentences about newlines survive. `remove_fillers`: regex um/uh/erm removal + punctuation/capitalization repair (no LLM); config `remove_fillers` (default true), checkbox in log window; log.jsonl always keeps raw text |
| Log & recovery | `logstore.py`, `gui.py`, `retry.py` | `recordings/log.jsonl`; Retry = async `/v2/transcript` |
| Tray | `tray.py` | pystray, runs detached |

## Ops
- Run: `pythonw main.py` (singleton via port 51733). Autostart: shortcut in `shell:startup`.
- **Restart with `pythonw main.py --restart`** — graceful takeover via the singleton socket (port 51733 doubles as control channel): the old instance finishes any in-flight recording (saved + logged, no paste), quits, and the new one takes the port. Never Stop-Process. (Verified: takeover during an active take waited for it to finish.)
- Log rows are written at recording START (`status: recording`) and updated at finish — a crash leaves the row stuck, and `logstore.recover_orphans()` at next startup repairs the WAV header, flips it to `recovered`, and pops the log window for Retry. (Verified with a deliberate mid-take kill.)
- Debug log: `app.log` (rotating not needed; small). Test scripts pattern: feed a WAV into `StreamSession` with a `queue.Queue` at 50 ms pacing.
- Costs money while streaming ($0.45/hr) — keep test clips short.
