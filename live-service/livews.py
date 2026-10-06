"""
Minimal F1 live-timing WebSocket client.

Why not FastF1's own SignalRClient? It calls
`POST https://livetiming.formula1.com/signalrcore/negotiate` first, and that
endpoint is intermittently unreachable (observed: 20 s timeouts for hours at a
time, while the WebSocket itself answered `101 Switching Protocols` in 0.4 s).
Opening the socket directly and speaking the SignalR JSON protocol by hand skips
the negotiate round-trip entirely and proved to work anonymously:

    handshake  -> {"protocol":"json","version":1}
    subscribe  -> {"type":1,"target":"Subscribe","arguments":[[...topics]]}
    server     -> {"type":1,"target":"feed","arguments":["Position.z", "<b64>"]}
    server     -> {"type":6}            (ping; must be answered with {"type":6})

Messages are record-separated by 0x1E. Payloads land in `LiveState`, which keeps
only the latest value per driver, so memory stays in the tens of megabytes.
"""
from __future__ import annotations

import json
import threading
import time

import websocket

from livefeed import LiveState

WS_URL = "wss://livetiming.formula1.com/signalrcore"
SEP = "\x1e"

TOPICS = [
    "Heartbeat",
    "DriverList",
    "SessionInfo",
    "SessionStatus",
    "TrackStatus",
    "WeatherData",
    "LapCount",
    "Position.z",
    "CarData.z",
    "TimingData",
    "TimingAppData",          # единственный топик со стинтами
    "RaceControlMessages",
    "TeamRadio",              # ключ "Captures", по одной записи на сообщение
    "TimingStats",
]

CONNECT_TIMEOUT = 25
RECV_TIMEOUT = 30


class LiveFeed:
    """Keeps a LiveState up to date from F1's live stream.

    Usage:
        feed = LiveFeed()
        feed.start()                       # background thread, reconnects itself
        feed.state.snapshot(...)           # read at any time
        feed.stop()
    """

    def __init__(self, topics=None, log=print):
        self.state = LiveState()
        self.topics = topics or TOPICS
        self.log = log
        self._ws = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.connected = False
        self.messages = 0
        self.last_message_at = None
        self._lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="f1-livefeed",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    # ── internals ──────────────────────────────────────────────────────────
    def _run(self):
        backoff = 2
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 2
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.connected = False
                self.log(f"live feed: соединение прервано ({type(exc).__name__}), "
                         f"переподключение через {backoff} с")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _session(self):
        with self._lock:
            self._ws = None
        ws = websocket.create_connection(WS_URL, timeout=CONNECT_TIMEOUT,
                                          suppress_origin=True)
        with self._lock:
            self._ws = ws
        self.connected = True
        self.log("live feed: соединение установлено")

        ws.send(json.dumps({"protocol": "json", "version": 1}) + SEP)
        ws.settimeout(RECV_TIMEOUT)
        ws.recv()                                   # handshake response ("{}")
        ws.send(json.dumps({"type": 1, "target": "Subscribe",
                            "arguments": [self.topics]}) + SEP)
        self.log(f"live feed: подписка на {len(self.topics)} топиков")

        while not self._stop.is_set():
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                # no traffic for RECV_TIMEOUT — keep the socket, it is still valid
                continue
            except Exception:
                raise
            if raw is None:
                raise ConnectionError("соединение закрыто сервером")
            for part in str(raw).split(SEP):
                if not part.strip():
                    continue
                try:
                    msg = json.loads(part)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == 6:                      # ping -> must answer
                    try:
                        ws.send(json.dumps({"type": 6}) + SEP)
                    except Exception:
                        pass
                    continue
                if msg.get("target") != "feed":
                    continue
                args = msg.get("arguments") or []
                if len(args) >= 2:
                    self.messages += 1
                    self.last_message_at = time.time()
                    self.state.feed(args[0], args[1])

        try:
            ws.close()
        except Exception:
            pass
        self.connected = False

    # ── diagnostics ────────────────────────────────────────────────────────
    def status(self):
        return {
            "connected": self.connected,
            "messages": self.messages,
            "idle_seconds": (round(time.time() - self.last_message_at, 1)
                             if self.last_message_at else None),
            "cars": len(self.state.cars),
            "drivers": len(self.state.drivers),
        }


if __name__ == "__main__":
    f = LiveFeed()
    f.start()
    try:
        for _ in range(9):
            time.sleep(10)
            print(" ", f.status())
    except KeyboardInterrupt:
        pass
    finally:
        f.stop()