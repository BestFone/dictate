"""AssemblyAI Universal-Streaming v3 websocket client.

Runs in its own thread: connects, drains the recorder's send_queue, collects
Turn messages. Connection failure never stops local recording — the controller
just sees connected=False and the recording is retried later via the async API.
"""
import json
import logging
import queue
import threading
import urllib.parse

import websocket

log = logging.getLogger("dictate.stream")

WS_BASE = "wss://streaming.assemblyai.com/v3/ws"


class StreamSession(threading.Thread):
    def __init__(self, api_key, send_queue, config, on_turn=None, on_status=None):
        super().__init__(daemon=True)
        self.api_key = api_key
        self.send_queue = send_queue
        self.config = config
        self.on_turn = on_turn          # called with (turn_dict) on every Turn message
        self.on_status = on_status      # called with 'connected' | 'failed' | 'closed'
        self.turns = {}                 # turn_order -> best transcript so far
        self.connected = False
        self.failed = False
        self.error = None
        self._stop_evt = threading.Event()
        self._terminated = threading.Event()
        self.ws = None

    def _url(self):
        params = [
            ("sample_rate", self.config.get("sample_rate", 16000)),
            ("encoding", "pcm_s16le"),
            ("speech_model", self.config.get("speech_model", "universal-3-5-pro")),
            ("mode", self.config.get("streaming_mode", "balanced")),
            ("format_turns", "true"),
            ("language_detection", "true"),
        ]
        for code in self.config.get("language_codes") or []:
            params.append(("language_codes", code))
        return WS_BASE + "?" + urllib.parse.urlencode(params)

    def run(self):
        try:
            self.ws = websocket.create_connection(
                self._url(),
                header={"Authorization": self.api_key},
                timeout=10,
            )
        except Exception as e:
            # Retry once without language_codes in case the serialization is rejected
            if self.config.get("language_codes"):
                log.warning("connect failed (%s); retrying without language_codes", e)
                try:
                    cfg = dict(self.config, language_codes=[])
                    self.config = cfg
                    self.ws = websocket.create_connection(
                        self._url(), header={"Authorization": self.api_key}, timeout=10
                    )
                except Exception as e2:
                    return self._fail(e2)
            else:
                return self._fail(e)

        self.connected = True
        if self.on_status:
            self.on_status("connected")
        log.info("websocket connected")

        recv = threading.Thread(target=self._recv_loop, daemon=True)
        recv.start()

        # Sender loop: drain audio queue until asked to stop, then Terminate.
        while not self._stop_evt.is_set():
            try:
                chunk = self.send_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as e:
                return self._fail(e)

        # Flush whatever audio is still queued, then terminate the session.
        try:
            while True:
                chunk = self.send_queue.get_nowait()
                self.ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
        except queue.Empty:
            pass
        except Exception as e:
            return self._fail(e)
        try:
            self.ws.send(json.dumps({"type": "Terminate"}))
        except Exception:
            pass
        self._terminated.wait(timeout=5)
        try:
            self.ws.close()
        except Exception:
            pass
        self.connected = False
        if self.on_status:
            self.on_status("closed")

    def _recv_loop(self):
        while True:
            try:
                raw = self.ws.recv()
            except Exception:
                if not self._stop_evt.is_set():
                    self._fail("receive loop dropped")
                self._terminated.set()
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            mtype = msg.get("type")
            if mtype == "Turn":
                order = msg.get("turn_order", 0)
                transcript = msg.get("transcript", "")
                # Prefer formatted end-of-turn text; otherwise keep latest partial
                # so an abrupt stop still yields whatever was heard.
                if transcript:
                    self.turns[order] = transcript
                if self.on_turn:
                    self.on_turn(msg)
            elif mtype == "Termination":
                log.info("session terminated: %s", msg)
                self._terminated.set()
                return
            elif mtype == "Begin":
                log.info("session began: %s", msg.get("id"))

    def _fail(self, err):
        self.failed = True
        self.connected = False
        self.error = str(err)
        log.error("stream failed: %s", err)
        self._terminated.set()
        if self.on_status:
            self.on_status("failed")

    def stop(self):
        """Signal end of audio; thread finishes flushing + terminating on its own."""
        self._stop_evt.set()

    def transcript(self):
        return " ".join(self.turns[k].strip() for k in sorted(self.turns) if self.turns[k].strip())
