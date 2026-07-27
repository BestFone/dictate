"""Microphone capture: 16 kHz mono int16 -> WAV file + streaming queue + RMS level."""
import audioop
import collections
import os
import queue
import threading
import time
import wave

import sounddevice as sd

import logstore

CHUNK_MS = 50


class Recorder:
    """Owns the mic stream and the on-disk WAV. Audio is never gated on the network:
    the WAV write happens in the audio callback path regardless of websocket state."""

    def __init__(self, sample_rate=16000, device=None):
        self.sample_rate = sample_rate
        self.device = device
        self.stream = None
        self.wav = None
        self.wav_path = None
        self.send_queue = queue.Queue()
        self.levels = collections.deque(maxlen=48)  # recent RMS values for the waveform
        self.started_at = None
        self.frames_written = 0
        self._lock = threading.Lock()

    def start(self, rec_id: str):
        os.makedirs(logstore.REC_DIR, exist_ok=True)
        self.wav_path = os.path.join(logstore.REC_DIR, f"{rec_id}.wav")
        self.wav = wave.open(self.wav_path, "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.sample_rate)
        self.frames_written = 0
        self.levels.clear()
        self.started_at = time.time()
        blocksize = int(self.sample_rate * CHUNK_MS / 1000)
        self.stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=self.device,
            callback=self._callback,
        )
        self.stream.start()

    def _callback(self, indata, frames, t, status):
        data = bytes(indata)
        with self._lock:
            if self.wav is not None:
                self.wav.writeframes(data)
                self.frames_written += frames
        self.send_queue.put(data)
        self.levels.append(audioop.rms(data, 2))

    def duration(self):
        return self.frames_written / self.sample_rate if self.sample_rate else 0.0

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None
        with self._lock:
            if self.wav is not None:
                self.wav.close()
                self.wav = None
        return self.wav_path
