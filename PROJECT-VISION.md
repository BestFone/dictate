# Dictate — project vision

## What we're building
A Wispr Flow clone for Windows with a tiny footprint (~50 MB vs Electron's ~400 MB): global-hotkey push-to-talk dictation that streams to AssemblyAI and pastes the transcript wherever the cursor is. Built in Python + tkinter so it can be patched in seconds.

## Who it's for
Avi, single user, on this Windows 10 machine. Dictating English with some Hebrew mixed in, into any app (Obsidian, browser, email).

## The end product
A tray app that autostarts at login. **Win+Ctrl+Space** starts recording — a floating pill appears bottom-center with a red dot, live waveform (proof the mic is actually picking up sound), and elapsed timer. Same hotkey (or clicking the pill) stops: the transcript lands on the clipboard and auto-pastes. **Esc** or the pill's ✕ cancels — no paste, but the audio is still saved and logged.

## Features
- **Must-have — never lose a recording**: every take is written to a local WAV from the first frame, independent of network state. Every take gets a row in the log window (tray → "Recordings log…") with Copy / Retry (async API re-transcription) / Play / Open folder. Failed streams pop the log window automatically.
- Streaming: AssemblyAI Universal-3.5 Pro Realtime ($0.45/hr), `language_codes=[en, he]`, Hebrew returned in Hebrew script.
- Two delivery modes: **paste-on-stop** (default; you can click around while recording, cursor position at stop wins) and **live typing** (each finalized phrase is typed into the focused window as you speak; toggle in tray or log window).
- Offline behavior: stream drops → pill turns amber "offline — recording locally", recording continues, entry marked `stream_failed` for one-click Retry.
- Cost ticker in the log window ($0.45/hr × total streamed minutes).

## Non-goals
- No multi-user, no packaging/installer, no custom vocabulary UI, no diarization.
- Not a meeting recorder — for long-form transcription use the vault's /transcribe tooling.

## Data & sensitivity
Recordings + `log.jsonl` live in `recordings/` next to the app, kept indefinitely (they are the recovery mechanism). Dictation may contain personal/legal content — folder stays local, nothing synced. API key read from `~/.config/assemblyai/api_key`.

## Config
`config.json`: `hotkey` (default `windows+ctrl+space` — the Wispr binding; change here if Windows IME fights it), `mode` (`paste`/`live`), `language_codes`, `input_device` (null = system default; see device list via `python -c "import sounddevice as sd; print(sd.query_devices())"`), `autopaste`, `streaming_mode` (`balanced`).
