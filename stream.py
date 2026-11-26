"""AssemblyAI streaming v3 websocket client (AssemblyAI-STT-3.7-Realtime).

Runs in its own thread: connects, drains the recorder's send_queue, collects
Turn messages. Connection failure never stops local recording — the controller
just sees failed=True and the recording is retried later via the async API.

Mid-take drops are survived by reconnecting: every chunk of the take is kept
in memory, and a new connection resends from the end of the last FINISHED turn
(its last word's timestamp), so nothing is lost or doubled. The server rejects
audio sent faster than real time (close 3007), so resent audio is paced, and
the stream stays behind by the backlog until the take ends.
"""
import json
import logging
import queue
import struct
import threading
import time
import urllib.parse

import websocket

log = logging.getLogger("dictate.stream")

WS_BASE = "wss://streaming.assemblyai.com/v3/ws"
MAX_RECONNECTS = 3          # per take; after that the async retry takes over
BACKOFF = (0.5, 1.0, 2.0)   # seconds before reconnect attempt 1, 2, 3
LEAD_SECS = 0.5             # how far audio may run ahead of session time (a fresh
                            # session always gets a ~0.3 s burst of queued audio)
# Measured 2026-11-24: the server transcribes at real time however fast audio
# arrives — 2.0 gave no speedup, and a 20 s burst just queued server-side. So
# the backlog after a reconnect can't be caught up; it's paid at stop instead.
CATCHUP_RATE = 1.0          # audio-seconds per wall-second
MAX_STOP_WAIT = 10          # backlog at stop above this → async retry is faster
MAX_CHUNK_MS = 100         # server accepts 20-1000 ms per message
MIN_CHUNK_MS = 20


class _Conn:
    """State of one websocket connection within a take."""
    def __init__(self, ws, start_byte):
        self.ws = ws
        self.start_byte = start_byte   # take-buffer offset of this session's audio 0 ms
        self.t0 = time.time()
        self.sent = 0                  # bytes sent on this connection
        self.last_final_end_ms = None  # end of the last word of the last ended turn
        self.base = 0                  # added to server turn_order (restarts at 0)
        self.error_text = None         # 'Error' message the server sends before closing
        self.reason = None             # why it dropped, for the log
        self.close_code = None
        self.dropped = threading.Event()
        self.terminated = threading.Event()


class StreamSession(threading.Thread):
    def __init__(self, api_key, send_queue, config, on_turn=None, on_status=None):
        super().__init__(daemon=True)
        self.api_key = api_key
        self.send_queue = send_queue
        self.config = config
        self.on_turn = on_turn          # called with (turn_dict) on every Turn message
        self.on_status = on_status      # 'connected' | 'reconnecting' | 'failed' | 'closed'
        self.turns = {}                 # take-wide turn order -> best transcript so far
        self._ended = set()             # take-wide orders whose turn ended
        self.connected = False
        self.failed = False
        self.error = None
        self.drops = []                 # reasons of survived drops (logged per take)
        self.audio = bytearray()        # the whole take, for resending after a drop
        self.pos = 0                    # next byte of self.audio to send
        self.bytes_per_ms = self.config.get("sample_rate", 16000) * 2 / 1000
        self._stop_evt = threading.Event()
        self._abort_evt = threading.Event()

    def _url(self):
        params = [
            ("sample_rate", self.config.get("sample_rate", 16000)),
            ("encoding", "pcm_s16le"),
            ("speech_model", self.config.get("speech_model", "assemblyai-stt-3-7-realtime")),
            ("mode", self.config.get("streaming_mode", "balanced")),
            ("format_turns", "true"),
            ("language_detection", "true"),
        ]
        for code in self.config.get("language_codes") or []:
            params.append(("language_codes", code))
        # Contextual prompt: DESCRIBES the audio (incl. its language), it is not
        # an instruction — AssemblyAI ignores behavioral/formatting commands.
        prompt = (self.config.get("prompt") or "").strip()
        if prompt:
            params.append(("prompt", prompt))
        return WS_BASE + "?" + urllib.parse.urlencode(params)

    def _connect(self):
        try:
            return websocket.create_connection(
                self._url(), header={"Authorization": self.api_key}, timeout=10)
        except Exception as e:
            if not self.config.get("language_codes"):
                raise
            # Retry once without language_codes in case the serialization is rejected
            log.warning("connect failed (%r); retrying without language_codes", e)
            self.config = dict(self.config, language_codes=[])
            return websocket.create_connection(
                self._url(), header={"Authorization": self.api_key}, timeout=10)

    def run(self):
        attempt = 0
        base = 0
        while not self._abort_evt.is_set():
            try:
                ws = self._connect()
            except Exception as e:
                reason = f"connect failed: {e!r}"
                if attempt >= MAX_RECONNECTS:
                    return self._fail(reason)
                log.warning("%s (attempt %d)", reason, attempt + 1)
                time.sleep(BACKOFF[attempt])
                attempt += 1
                continue
            conn = _Conn(ws, self.pos)
            conn.base = base
            self.connected = True
            if self.on_status:
                self.on_status("connected")
            log.info("websocket connected%s", f" (reconnect {attempt}, resending "
                     f"{(len(self.audio) - self.pos) / self.bytes_per_ms / 1000:.1f}s)"
                     if attempt else "")
            done = self._run_connection(conn)
            try:
                ws.close()
            except Exception:
                pass
            self.connected = False
            if done:
                break
            reason = conn.reason or "dropped"
            log.error("stream dropped: %s", reason)
            # 1008 = auth/billing/rate-limit: reconnecting now can't help.
            if conn.close_code == 1008 or attempt >= MAX_RECONNECTS:
                return self._fail(reason)
            self.drops.append(reason)
            self._rewind(conn)
            base = max(self.turns, default=-1) + 1
            attempt += 1
            if self.on_status:
                self.on_status("reconnecting")
            time.sleep(BACKOFF[attempt - 1])
        if self.drops:
            self.error = f"reconnected {len(self.drops)}x: " + "; ".join(self.drops)
        if self.on_status:
            self.on_status("closed")

    def _rewind(self, conn):
        """Resend from the end of the last finished turn; throw away the partial
        turn after it — the new session transcribes that audio again."""
        if conn.last_final_end_ms is not None:
            back = int(conn.last_final_end_ms * self.bytes_per_ms) & ~1
            self.pos = min(conn.start_byte + back, len(self.audio))
        else:
            self.pos = conn.start_byte
        for order in [o for o in self.turns if o >= conn.base and o not in self._ended]:
            del self.turns[order]

    def _pump(self):
        try:
            while True:
                self.audio += self.send_queue.get_nowait()
        except queue.Empty:
            pass

    def _run_connection(self, conn):
        """Send audio until the take ends. True = clean Termination, False = dropped."""
        recv = threading.Thread(target=self._recv_loop, args=(conn,), daemon=True)
        recv.start()
        max_chunk = int(MAX_CHUNK_MS * self.bytes_per_ms)
        min_chunk = int(MIN_CHUNK_MS * self.bytes_per_ms)
        terminate_deadline = None
        while True:
            if conn.terminated.is_set() or self._abort_evt.is_set():
                return True
            if conn.dropped.is_set():
                return False
            if terminate_deadline:
                if time.time() > terminate_deadline:
                    log.warning("no Termination within 5 s; closing anyway")
                    return True
                time.sleep(0.05)
                continue
            self._pump()
            stopping = self._stop_evt.is_set()
            avail = len(self.audio) - self.pos
            # Never ahead of real time on this connection (server closes with 3007).
            allowed = int(((time.time() - conn.t0) * CATCHUP_RATE + LEAD_SECS)
                          * 1000 * self.bytes_per_ms - conn.sent) & ~1
            n = min(avail, max_chunk, allowed)
            if n >= min_chunk or (stopping and 0 < n == avail):
                chunk = bytes(self.audio[self.pos:self.pos + n])
                if len(chunk) < min_chunk:   # final scrap: pad to the 20 ms minimum
                    chunk += b"\0" * (min_chunk - len(chunk))
                try:
                    conn.ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
                except Exception as e:
                    conn.reason = conn.reason or f"send failed: {e!r}"
                    conn.dropped.set()
                    return False
                self.pos += n
                conn.sent += len(chunk)
                continue
            if stopping and avail == 0:
                try:
                    conn.ws.send(json.dumps({"type": "Terminate"}))
                except Exception as e:
                    conn.reason = conn.reason or f"send Terminate failed: {e!r}"
                    conn.dropped.set()
                    return False
                terminate_deadline = time.time() + 5
                continue
            time.sleep(0.02)

    def _recv_loop(self, conn):
        while True:
            try:
                opcode, frame = conn.ws.recv_data_frame(True)
            except websocket.WebSocketTimeoutException:
                # Benign: the server sends nothing during silence between turns,
                # so the 10 s socket timeout fires on any long thinking pause.
                continue
            except Exception as e:
                if not conn.terminated.is_set():
                    conn.reason = conn.reason or self._why(f"receive failed: {e!r}", conn)
                    conn.dropped.set()
                return
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                data = frame.data or b""
                code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else None
                text = data[2:].decode("utf-8", "replace")
                conn.close_code = code
                log.warning("server closed: code=%s reason=%r", code, text)
                if not conn.terminated.is_set():
                    conn.reason = self._why(f"close {code}: {text}".strip(), conn)
                    conn.dropped.set()
                return
            if opcode not in (websocket.ABNF.OPCODE_TEXT, websocket.ABNF.OPCODE_BINARY):
                continue   # ping/pong (pong already answered by the library)
            try:
                msg = json.loads(frame.data)
            except (TypeError, ValueError):
                continue
            if conn.dropped.is_set():
                return   # a late message from a dead connection; the next one owns turns
            mtype = msg.get("type")
            if mtype == "Turn":
                # Server turn_order restarts at 0 per connection; keep it take-wide.
                order = conn.base + msg.get("turn_order", 0)
                msg["turn_order"] = order
                transcript = msg.get("transcript", "")
                # Prefer formatted end-of-turn text; otherwise keep latest partial
                # so an abrupt stop still yields whatever was heard.
                if transcript:
                    self.turns[order] = transcript
                if msg.get("end_of_turn"):
                    self._ended.add(order)
                    ends = [w.get("end") for w in msg.get("words") or [] if w.get("end")]
                    if ends:
                        conn.last_final_end_ms = max(ends)
                if self.on_turn:
                    self.on_turn(msg)
            elif mtype == "Error":
                conn.error_text = msg.get("error") or json.dumps(msg)
                log.warning("server error message: %s", conn.error_text)
            elif mtype == "Termination":
                log.info("session terminated: %s", msg)
                conn.terminated.set()
                return
            elif mtype == "Begin":
                log.info("session began: %s", msg.get("id"))

    @staticmethod
    def _why(reason, conn):
        return f"{reason} ({conn.error_text})" if conn.error_text else reason

    def _fail(self, err):
        self.failed = True
        self.connected = False
        self.error = "; ".join(self.drops + [str(err)])
        log.error("stream failed: %s", err)
        if self.on_status:
            self.on_status("failed")

    def stop(self):
        """Signal end of audio; thread finishes flushing + terminating on its own."""
        self._stop_evt.set()

    def abort(self):
        """Hang up without sending the rest (the caller recovers from the WAV)."""
        self._abort_evt.set()
        self._stop_evt.set()

    def backlog_secs(self):
        """Audio not yet sent — after a reconnect this is sent at real time."""
        return max(0, len(self.audio) + self.send_queue.qsize() * 1600 - self.pos) \
            / self.bytes_per_ms / 1000

    def transcript(self):
        return " ".join(self.turns[k].strip() for k in sorted(self.turns) if self.turns[k].strip())
