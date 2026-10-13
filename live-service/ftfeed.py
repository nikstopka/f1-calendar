"""Live timing via formula-timing.com's open SSE endpoint.

Why this exists
---------------
F1's own host refuses us: `livetiming.formula1.com` answers 403 from CloudFront,
on the WebSocket handshake and on every HTTP path, from two different networks
and four header variants. Verified again on 13 October 2026.

formula-timing.com republishes the same feed and — unlike its own pages, which
sit behind a Cloudflare challenge — its `/api/livetiming` answers a plain HTTP
client. Their front end reads it the same way, from the browser:

    new EventSource(`${NEXT_PUBLIC_LIVE_URL}/api/livetiming`)
      .addEventListener("initial", …)   // snapshot
      .addEventListener("update",  …)   // live deltas

What it costs us
----------------
They rewrite the payloads: camelCase keys, and `Entries` wrappers already
unwrapped. So this module maps their shape back into the shape the handlers in
livefeed.py expect, and feeds the existing `LiveState`. Nothing downstream —
snapshot(), the JSON endpoints, the site — changes.

What is unknown
---------------
Only `initial` has ever been observed, and only for a finished session. `update`
has never been seen, because no session is running. So:

  * every unrecognised key is counted and logged once, rather than dropped
    silently — on 25 October, when a session runs, the log will name what
    arrived instead of anyone having to guess;
  * anything unrecognised is left out of the snapshot rather than guessed at, so
    a wrong shape shows up as a missing field, not as wrong data on screen.

No coordinates so far: the `initial` payload carries none, and whether `update`
brings them is the open question. When they do appear they map onto CarData.z and
Position.z unchanged.
"""
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

from livefeed import LiveState

ENDPOINT = "https://formula-timing.com/api/livetiming"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

CONNECT_TIMEOUT = 25
READ_TIMEOUT = 30
# No traffic for this long means the socket is half-open, not idle. Borrowed from
# julesr0y/f1-livetiming-api, which watches 20s and forces a reconnect. Without
# it a dead socket looks healthy forever: our own loop would keep reporting
# "connected" while the map stood still.
SILENT_LIMIT = 25


def _text(v):
    """Lap times arrive as {"value": "1:36.201"}; handlers want {"Value": …}."""
    if isinstance(v, dict):
        val = v.get("value") or v.get("Value")
        return {"Value": val} if val else None
    return {"Value": v} if v else None


class FormulaTimingFeed:
    """Same surface as livews.LiveFeed: start, stop, status, .state."""

    def __init__(self, log=print):
        self.log = log
        self.state = LiveState()
        self.connected = False
        self.messages = 0
        self.last_message_at = 0.0
        self.source = "formula-timing.com/api/livetiming"
        self.errors = 0
        self.unknown = Counter()
        self.started_status = None
        self._stop = threading.Event()
        self._thread = None

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.connected = False

    def status(self):
        return {
            "connected": self.connected,
            "messages": self.messages,
            "idle_seconds": (round(time.time() - self.last_message_at, 1)
                             if self.last_message_at else None),
            "cars": len(self.state.cars),
            "drivers": len(self.state.drivers),
            "source": self.source,
            "unknown_keys": dict(self.unknown),
        }

    @property
    def is_live(self):
        """True while the session is actually running.

        A finished session still streams its snapshot, so liveness has to come
        from the status flag and not from having received data.
        """
        return self.started_status not in (None, "", "Finished")

    # ── reading ─────────────────────────────────────────────────────────────
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
                self.errors += 1
                self.log(f"formula-timing: поток прерван ({type(exc).__name__}), "
                         f"повтор через {backoff} с")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)

    def _session(self):
        req = urllib.request.Request(ENDPOINT, headers={
            "User-Agent": UA,
            "Accept": "text/event-stream",
        })
        resp = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT)
        self.connected = True
        self.log("formula-timing: поток открыт")

        last_any = time.time()
        event = None
        data = []
        try:
            while not self._stop.is_set():
                try:
                    raw = resp.readline()
                except socket.timeout:
                    if time.time() - last_any > SILENT_LIMIT:
                        raise ConnectionError("нет трафика дольше %d с" % SILENT_LIMIT)
                    continue
                if not raw:
                    raise ConnectionError("поток закрыт сервером")
                last_any = time.time()
                line = raw.decode("utf-8", "replace").rstrip("\n")

                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    data.append(line.split(":", 1)[1].strip())
                elif line == "":
                    if data:
                        self.messages += 1
                        self.last_message_at = time.time()
                        self._handle(event or "message", "\n".join(data))
                    event, data = None, []
        finally:
            self.connected = False
            try:
                resp.close()
            except Exception:
                pass

    # ── mapping ─────────────────────────────────────────────────────────────
    def _handle(self, event, raw):
        try:
            payload = json.loads(raw)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        if event == "initial":
            self.state.reset()
            self.log("formula-timing: получен снимок, ключей %d" % len(payload))
        self._apply(payload)

    def _apply(self, p):
        """Feed the existing handlers, in the shape they expect."""
        st = self.state

        # ── identity ──
        si = p.get("sessionInfo")
        if isinstance(si, dict):
            st.feed("SessionInfo", json.dumps({
                "Key": si.get("key") or si.get("Key"),
                "Name": si.get("name") or si.get("Name"),
                "Type": si.get("type") or si.get("Type"),
                "Path": si.get("path") or si.get("Path"),
                "StartDate": si.get("startDate"),
                "EndDate": si.get("endDate"),
                "Meeting": si.get("meeting") or {},
            }))
            circuit = (si.get("meeting") or {}).get("circuit") or {}
            if circuit.get("key"):
                st.session_info["circuit_key"] = circuit["key"]

        ss = p.get("sessionStatus")
        if isinstance(ss, dict):
            self.started_status = ss.get("started") or ss.get("Status")

        dl = p.get("driverList")
        if isinstance(dl, dict) and dl:
            out = {}
            for key, e in dl.items():
                if not isinstance(e, dict):
                    continue
                out[key] = {
                    "RacingNumber": e.get("racingNumber") or key,
                    "FullName": e.get("fullName") or "",
                    "Tla": e.get("tla") or "",
                    "TeamName": e.get("teamName") or "",
                    "TeamColour": e.get("teamColour") or "",
                    "BroadcastName": e.get("broadcastName") or "",
                }
            st.feed("DriverList", json.dumps(out))

        # ── timing table, plus the tyre currently fitted ──
        # The tyre does not live in timingData: it has to be read off the stint
        # that covers the current lap, so both topics are needed together.
        compounds = self._stint_compounds(p.get("timingAppData"))
        td = p.get("timingData")
        if isinstance(td, dict) and td.get("lines"):
            lines = {}
            for key, l in td["lines"].items():
                if not isinstance(l, dict):
                    continue
                row = {
                    "Position": l.get("position"),
                    "NumberOfLaps": l.get("numberOfLaps"),
                    "GapToLeader": l.get("gapToLeader"),
                    "NumberOfPitStops": l.get("numberOfPitStops"),
                    "Retired": l.get("retired"),
                    "Stopped": l.get("stopped"),
                }
                bl = _text(l.get("bestLapTime"))
                if bl:
                    row["BestLapTime"] = bl
                ll = _text(l.get("lastLapTime"))
                if ll:
                    row["LastLapTime"] = ll
                iv = l.get("intervalToPositionAhead")
                if isinstance(iv, dict) and iv.get("value"):
                    row["IntervalToPositionAhead"] = {"Value": iv["value"]}
                comp = compounds.get(str(key))
                if comp:
                    row["Tyres"] = {"0": comp}
                lines[str(key)] = row
            st.feed("TimingData", json.dumps({"Lines": lines}))

        ta = p.get("timingAppData")
        if isinstance(ta, dict) and ta.get("lines"):
            out = {}
            for key, l in ta["lines"].items():
                if not isinstance(l, dict):
                    continue
                stints = []
                for s in (l.get("stints") or []):
                    if not isinstance(s, dict):
                        continue
                    stints.append({
                        "Compound": s.get("compound"),
                        "StartLap": s.get("startLaps"),
                        "EndLap": s.get("totalLaps"),
                        "LapNumber": s.get("lapNumber"),
                        "New": s.get("new"),
                        "LapTime": s.get("lapTime"),
                    })
                out[str(key)] = {"Stints": stints, "Line": l.get("line")}
            st.feed("TimingAppData", json.dumps({"Lines": out}))

        # ── conditions ──
        w = p.get("weatherData")
        if isinstance(w, dict) and w:
            st.feed("WeatherData", json.dumps({"Entries": [{
                "Utc": p.get("heartbeat", {}).get("utc") if isinstance(p.get("heartbeat"), dict) else None,
                "AirTemp": w.get("airTemp"), "TrackTemp": w.get("trackTemp"),
                "Humidity": w.get("humidity"), "WindSpeed": w.get("windSpeed"),
                "WindDirection": w.get("windDirection"),
                "Rainfall": w.get("rainfall"), "Pressure": w.get("pressure"),
            }]}))

        ts = p.get("trackStatus")
        if isinstance(ts, dict) and ts:
            st.feed("TrackStatus", json.dumps({"Entries": [{
                "Status": ts.get("status"), "Message": ts.get("message")}]}))

        lc = p.get("lapCount")
        if isinstance(lc, dict) and lc:
            st.feed("LapCount", json.dumps({"Entries": [{
                "CurrentLap": lc.get("currentLap"), "TotalLaps": lc.get("totalLaps")}]}))

        rc = p.get("raceControlMessages")
        if isinstance(rc, dict) and rc.get("messages"):
            st.feed("RaceControlMessages",
                    json.dumps({"Messages": rc["messages"]}))

        tr = p.get("teamRadio")
        if isinstance(tr, dict) and tr.get("captures"):
            caps = []
            for c in tr["captures"]:
                if isinstance(c, dict):
                    caps.append({
                        "Utc": c.get("utc") or c.get("Utc"),
                        "RacingNumber": c.get("racingNumber") or c.get("RacingNumber"),
                        "Path": c.get("path") or c.get("Path"),
                    })
            if caps:
                st.feed("TeamRadio", json.dumps({"Captures": caps}))

        # ── coordinates, if they ever arrive ──
        for src, topic in (("carData", "CarData.z"), ("CarData", "CarData.z"),
                           ("Position", "Position.z"), ("position", "Position.z")):
            block = p.get(src)
            if isinstance(block, dict) and block:
                st.feed(topic, json.dumps(block))
                break

        for key in p:
            if key in _KNOWN:
                continue
            if not self.unknown[key]:
                self.log("formula-timing: незнакомый ключ %r — разбирать на "
                         "следующей сессии" % key)
            self.unknown[key] += 1

    @staticmethod
    def _stint_compounds(ta):
        """driver number -> compound of the stint covering the newest lap.

        Their snapshot lists stints without saying which is current, so the last
        one is used; that is the freshest tyre after a pit stop, which is what
        the tower shows.
        """
        out = {}
        if not isinstance(ta, dict):
            return out
        for key, l in (ta.get("lines") or {}).items():
            if not isinstance(l, dict):
                continue
            stints = [s for s in (l.get("stints") or [])
                      if isinstance(s, dict) and s.get("compound")]
            if stints:
                out[str(key)] = stints[-1].get("compound")
        return out


_KNOWN = {
    "sessionInfo", "sessionStatus", "sessionData", "driverList", "timingData",
    "timingStats", "timingAppData", "lapCount", "trackStatus", "weatherData",
    "raceControlMessages", "teamRadio", "teamRadioTranscripts", "heartbeat",
    "extrapolatedClock", "topThree", "pitLaneTimeCollection", "audioStreams",
    "contentStreams", "carData", "CarData", "Position", "position",
}