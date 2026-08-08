"""Re-submit a saved WAV through AssemblyAI's async API (recovery path)."""
import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("dictate.retry")

API = "https://api.assemblyai.com/v2"


def _req(url, api_key, data=None, headers=None, method=None):
    h = {"Authorization": api_key}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            body = err.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {err.code}: {body or err.reason}") from None


def transcribe_file(wav_path, api_key, language_codes=None):
    """Upload + transcribe + poll. Returns transcript text. Raises on failure."""
    with open(wav_path, "rb") as f:
        audio = f.read()
    up = _req(API + "/upload", api_key, data=audio,
              headers={"Content-Type": "application/octet-stream"})
    body = {
        "audio_url": up["upload_url"],
        # Plural + full model names — bare "speech_model": "universal" 400s,
        # and the API 400s on deprecated names too (it names the replacements).
        "speech_models": ["universal-3-5-pro", "universal-2"],
        "language_detection": True,
    }
    job = _req(API + "/transcript", api_key,
               data=json.dumps(body).encode("utf-8"),
               headers={"Content-Type": "application/json"})
    tid = job["id"]
    log.info("async transcript job %s for %s", tid, wav_path)
    while True:
        time.sleep(2)
        st = _req(f"{API}/transcript/{tid}", api_key)
        if st["status"] == "completed":
            return st.get("text") or ""
        if st["status"] == "error":
            raise RuntimeError(st.get("error", "transcription error"))
