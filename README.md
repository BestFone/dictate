# Dictate — push-to-talk dictation for Windows

A tiny Wispr-Flow-style dictation app: hit a global hotkey, talk, and the transcript is typed or pasted wherever your cursor is. Streams to [AssemblyAI Universal-Streaming](https://www.assemblyai.com/) (~$0.45/hr of audio). Python + tkinter, ~50 MB of RAM, no Electron.

- **Win+Ctrl+Space** toggles recording (configurable). A small pill overlay shows a live waveform.
- Click the pill or ✓ to finish; **Esc** or ✕ cancels — audio is *always* saved locally either way.
- Two delivery modes (toggle on the pill): **live** (types as you speak) or **paste** (clipboard + Ctrl+V at the end).
- Spoken "new line" / "new paragraph" become real line breaks; um/uh fillers are removed (configurable).
- Every take is saved as WAV + logged to `recordings/log.jsonl`. If the network drops or the app crashes mid-take, nothing is lost — the log window offers a Retry that re-submits the saved audio through AssemblyAI's async API.
- Multilingual: set `language_codes` (e.g. `["en", "he"]` for mixed English/Hebrew — Hebrew comes back in Hebrew script).

See `PROJECT-VISION.md` for the full design notes.

## Requirements

- Windows 10/11
- Python 3.10–3.12 (3.13 removed the `audioop` module this app uses)

## Setup

```powershell
git clone https://github.com/BestFone/dictate.git
cd dictate
pip install -r requirements.txt
```

### API key

Get a free API key at [assemblyai.com](https://www.assemblyai.com/) (streaming is pay-as-you-go, ~$0.45/hr). Provide it one of three ways (checked in this order):

1. `config.json` → `"api_key": "your-key-here"` (the file is gitignored, so it won't be committed)
2. Environment variable `ASSEMBLYAI_API_KEY`
3. A file at `~/.config/assemblyai/api_key` containing just the key

If no key is found, the app shows a dialog explaining exactly this instead of starting.

### Config

On first run, `config.json` is created automatically from `config.example.json`. Edit it to taste:

| Key | Default | Meaning |
|---|---|---|
| `hotkey` | `windows+ctrl+space` | Global push-to-talk toggle |
| `mode` | `live` | `live` types as you speak; `paste` delivers via clipboard at the end |
| `language_codes` | `["en"]` | Languages to detect, e.g. `["en", "he"]` for an English/Hebrew mix |
| `input_device` | `null` | `null` = system default microphone (see below) |
| `autopaste` | `true` | In paste mode, press Ctrl+V for you (vs just filling the clipboard) |
| `streaming_mode` | `balanced` | AssemblyAI latency-vs-accuracy tradeoff, passed through as-is |

### Finding your microphone

`null` uses the system default input. To pick a specific device, list them:

```powershell
python -m sounddevice
```

Copy the name (or the index number) of the input device you want into `input_device`, e.g. `"Logi C270 HD WebCam, MME"`. A partial unique name substring also works.

## Run

```powershell
pythonw main.py
```

`pythonw` runs it without a console window; it lives in the system tray. Only one instance runs at a time — launching again while it's running is a no-op, and `pythonw main.py --restart` gracefully replaces a running instance (finishing any in-flight recording first).

**Autostart:** put a shortcut to `pythonw main.py` in your Startup folder (Win+R → `shell:startup`).

## Privacy note

All recordings and transcripts are kept locally in `recordings/` (gitignored) as the crash-recovery mechanism. Audio is sent to AssemblyAI for transcription — see their [privacy policy](https://www.assemblyai.com/legal/privacy-policy) if that matters for your use.

## License

MIT — see [LICENSE](LICENSE).
