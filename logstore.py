"""Append-only JSONL log of recordings, with in-place status updates."""
import json
import logging
import os
import struct
import tempfile
import threading
import wave

log = logging.getLogger("dictate.logstore")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.path.join(APP_DIR, "recordings")
LOG_PATH = os.path.join(REC_DIR, "log.jsonl")

_lock = threading.Lock()


def _ensure_dir():
    os.makedirs(REC_DIR, exist_ok=True)


def append(entry: dict):
    _ensure_dir()
    with _lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_all():
    if not os.path.exists(LOG_PATH):
        return []
    entries = []
    with _lock:
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries


def _repair_wav(path):
    """Return duration, fixing the header first if a hard kill left the
    wave-module placeholder sizes in place. None if the file is unusable."""
    size = os.path.getsize(path)
    if size <= 44:  # header only, no audio
        return None
    try:
        with wave.open(path) as w:
            duration = w.getnframes() / (w.getframerate() or 16000)
        if duration > 0:
            return duration
    except Exception:
        pass
    # Patch RIFF (offset 4) and data-chunk (offset 40) lengths from file size.
    with open(path, "r+b") as f:
        f.seek(4)
        f.write(struct.pack("<I", size - 8))
        f.seek(40)
        f.write(struct.pack("<I", size - 44))
    return (size - 44) / 32000  # 16 kHz mono s16


def recover_orphans():
    """Called at startup. Rows are logged at recording START, so a take that
    died with the app shows up stuck in status 'recording' — salvage it.
    WAVs with no row at all (pre-log crash) are the paranoid second net."""
    if not os.path.isdir(REC_DIR):
        return 0
    count = 0
    entries = read_all()
    for e in entries:
        if e.get("status") != "recording":
            continue
        path = e.get("wav", "")
        duration = _repair_wav(path) if os.path.exists(path) else None
        if duration is None:
            update(e["id"], status="lost", error="app died; no audio on disk")
        else:
            update(e["id"], status="recovered", duration=round(duration, 1),
                   error="app died mid-recording; audio salvaged — use Retry")
            count += 1
        log.warning("salvaged stuck recording %s", e["id"])
    known = {e.get("id") for e in entries}
    for name in os.listdir(REC_DIR):
        if not name.endswith(".wav") or name[:-4] in known:
            continue
        path = os.path.join(REC_DIR, name)
        duration = _repair_wav(path)
        if duration is None:
            continue
        append({"id": name[:-4], "wav": path, "transcript": "",
                "status": "recovered", "duration": round(duration, 1),
                "model": None, "error": "orphan WAV found; audio salvaged"})
        log.warning("recovered orphan recording %s (%.1fs)", name[:-4], duration)
        count += 1
    return count


def update(entry_id: str, **fields):
    """Rewrite the log with the matching entry updated. Small file; fine."""
    with _lock:
        if not os.path.exists(LOG_PATH):
            return
        entries = []
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("id") == entry_id:
                    e.update(fields)
                entries.append(e)
        fd, tmp = tempfile.mkstemp(dir=REC_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, LOG_PATH)
