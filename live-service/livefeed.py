"""
Low-memory live state tracker for F1 live timing.

Instead of building pandas DataFrames from a whole session (FastF1's
`Session.load`, measured at ~780 MB for one race), this keeps only the latest
value per driver. Memory stays around a few MB.

Feed it raw messages straight from F1's SignalR stream:

    tracker.feed("Position.z", payload)

Payloads arrive in two encodings, which `decode_payload` handles:
  * base64 + raw deflate  — topics `Position.z`, `CarData.z`
  * plain JSON            — everything else (TimingData, WeatherData, …)

For offline verification the same tracker replays F1's own static archive, which
uses byte-identical encoding:

    https://livetiming.formula1.com/static/<path>/<Topic>.jsonStream
"""
from __future__ import annotations

import base64
import json
import re
import zlib

# CarData channel ids -> meaning (F1 live timing spec)
CAR_CHANNELS = {0: "rpm", 2: "throttle", 3: "brake", 4: "speed", 5: "gear"}

_TS_PREFIX = re.compile(r"^[\d:.]+\s*")
_QUOTED = re.compile(r'^[\d:.]+"([^"]*)"')


def decode_payload(raw: str):
    """Decode one message payload into Python data.

    Three shapes reach this function:

      1. `<ts>"<base64>"`  — static archive topic files (Position.z, CarData.z)
      2. `<ts><json>`      — archive lines for plain-JSON topics
      3. `<base64>`        — the live SignalR socket, which sends the compressed
                             payload with no timestamp prefix at all

    All three must work: the same tracker serves the archive replay in tests and
    the live feed in production. Returns None when the payload cannot be
    understood, so a single odd message never kills the feed.
    """
    if not raw:
        return None
    raw = raw.lstrip("\ufeff")               # first archive line carries a BOM
    m = _QUOTED.match(raw)
    if m:
        return _inflate(m.group(1))
    body = _TS_PREFIX.sub("", raw, count=1).strip()
    if body.startswith(("{", "[")):
        try:
            return json.loads(body)
        except Exception:
            return None
    return _inflate(body)                    # shape 3: bare base64


def _inflate(blob: str):
    """base64 -> raw zlib (no header) -> JSON. None on any failure."""
    blob = blob.strip()
    if not blob:
        return None
    try:
        blob += "=" * (-len(blob) % 4)          # tolerate missing padding
        data = zlib.decompress(base64.b64decode(blob), -zlib.MAX_WBITS)
        return json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None


def _as_list(value):
    """F1 mixes shapes: some topics send a list, others a dict keyed by id."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _entries(data):
    """Yield the payload's records.

    Some topics wrap their payload as {"Entries": [ ... ]} (Position.z,
    TimingData) while others send a bare object straight to the top level
    (WeatherData, TrackStatus, LapCount) or a dict keyed by id
    (RaceControlMessages). This normalises all of them.
    """
    if not isinstance(data, dict):
        return []
    for key in ("Entries", "Position", "Messages", "Lines"):
        if key in data:
            return _as_list(data[key])
    return [data]


def _num(v):
    try:
        if v != v:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _stint_count(raw):
    """Current stint number from a TimingAppData `Stints` field.

    F1 encodes the same stint table two ways:

      * the stream sends a dict keyed by stint index — {"0": {...}, "2": {...}}
      * the full state dump sends a list in the same order

    So the count is max(key) + 1 for the dict and len(list) for the list; both
    give 3 for a driver who ran MEDIUM, then SOFT, then SOFT.

    The stream is incremental — a message carries only the stint currently being
    updated — so the caller must keep the largest value seen, not the last one.
    Returns None when the field is absent, so a driver who has not pitted stays
    absent instead of showing a false zero.
    """
    if isinstance(raw, dict):
        idx = [int(k) for k in raw if str(k).lstrip("-").isdigit()]
        return max(idx) + 1 if idx else None
    items = _as_list(raw)
    return len(items) or None


def _newest_stint(raw):
    """The stint entry currently on the car, as a dict ({} when unknown)."""
    if isinstance(raw, dict):
        best = None
        for k, v in raw.items():
            if not isinstance(v, dict) or not str(k).lstrip("-").isdigit():
                continue
            if best is None or int(k) > best[0]:
                best = (int(k), v)
        return best[1] if best else {}
    items = [it for it in _as_list(raw) if isinstance(it, dict)]
    return items[-1] if items else {}


def lap_seconds(value):
    """Parse F1 lap-time strings into seconds.

    Accepts "1:44.916", "1:44:09.123" and a bare number. Empty strings and
    placeholders ("--.---") return None.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("Value")
        if value is None:
            return None
    text = str(value).strip()
    if not text or text in {"--.---", "--:--.---", "-"}:
        return None
    try:
        parts = text.split(":")
        total = 0.0
        for p in parts:
            total = total * 60 + float(p)
        return round(total, 3) if total > 0 else None
    except (TypeError, ValueError):
        return None


class LiveState:
    """Latest known state for one session. Thread-unsafe; single writer."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.cars: dict[int, dict] = {}        # driver_number -> {x,y,z,status,speed,...}
        self.positions: dict[int, int] = {}     # driver_number -> race position
        self.lap: dict[int, int] = {}           # driver_number -> current lap
        self.gap: dict[int, str] = {}           # driver_number -> gap text
        self.last_lap_time: dict[int, float] = {}
        self.best_lap: dict[int, dict] = {}
        self.tyre: dict[int, str] = {}
        self.stint: dict[int, int] = {}
        self.pit_stops: dict[int, int] = {}   # NumberOfPitStops, straight from F1
        self.pit: list = []                   # one event per counter increase
        self.drivers: dict[int, dict] = {}
        self.weather: dict = {}
        self.track_status: str | None = None
        self.lap_count: dict = {}
        self.race_control: list = []
        # Team radio captures: {t, n, path}. The audio URL is assembled at
        # snapshot time because SessionInfo.Path can arrive after the first
        # radio message.
        self.radio: list = []
        self.session_info: dict = {}
        self.updated: str | None = None

    # ── dispatch ───────────────────────────────────────────────────────────
    def feed(self, topic: str, raw: str) -> bool:
        data = decode_payload(raw)
        if data is None:
            return False
        handler = {
            "Position.z": self._on_position,
            "CarData.z": self._on_cardata,
            "TimingData": self._on_timing,
            "TimingStats": self._on_timing,
            "TimingAppData": self._on_timing_app,
            "WeatherData": self._on_weather,
            "TrackStatus": self._on_track,
            "LapCount": self._on_lapcount,
            "RaceControlMessages": self._on_rc,
            "TeamRadio": self._on_team_radio,
            "DriverList": self._on_drivers,
            "SessionInfo": self._on_session_info,
        }.get(topic)
        if handler is None:
            return False
        try:
            handler(data)
        except Exception:
            return False
        return True

    # ── handlers ───────────────────────────────────────────────────────────
    def _on_position(self, data):
        # {"Position": [{"Timestamp": ..., "Entries": {num: {...}}}]}
        for frame in _entries(data):
            if not isinstance(frame, dict):
                continue
            self.updated = frame.get("Timestamp")
            for num, e in (frame.get("Entries") or {}).items():
                n = int(num)
                car = self.cars.setdefault(n, {})
                car["x"] = _num(e.get("X"))
                car["y"] = _num(e.get("Y"))
                car["z"] = _num(e.get("Z"))
                car["status"] = e.get("Status")
                car["t"] = frame.get("Timestamp")

    def _on_cardata(self, data):
        # {"Entries": [{"Utc": ..., "Cars": {num: {"Channels": {...}}}]}]}
        for frame in _entries(data):
            if not isinstance(frame, dict):
                continue
            self.updated = frame.get("Utc")
            for num, e in (frame.get("Cars") or {}).items():
                car = self.cars.setdefault(int(num), {})
                for ch, name in CAR_CHANNELS.items():
                    v = (e.get("Channels") or {}).get(str(ch))
                    if v is not None:
                        car[name] = _num(v)

    def _record_pit_stop(self, n, new_count):
        """Append a pit event each time F1 raises a driver's stop counter.

        The `InPit`/`PitOut` flags look tempting but are not usable: over the
        Baku race they toggle on approach (InPit true/false/true...) and only the
        `NumberOfPitStops` counter moves exactly once per real stop. So the
        counter is the signal, and the event is written at that moment.

        `dur` stays None: the live feed has no stationary stop time, and the UI
        shows "—" rather than an invented number. `t` is the feed's own update
        stamp, so it is the session clock and not the host clock.
        """
        self.pit.append({
            "n": n,
            "lap": self.lap.get(n),
            "t": self.updated,
            "dur": None,
            "lane": None,
        })
        # NB: `del lst[-200:]` on a shorter list would wipe it entirely, so trim
        # by slicing assignment like the race-control list does.
        if len(self.pit) > 200:
            self.pit = self.pit[-200:]

    def _on_timing(self, data):
        # {"Lines": {num: {"Position": n, "NumberOfLaps": n, "GapToLeader": "...", ...}}}
        lines = data.get("Lines") if isinstance(data, dict) else None
        for num, l in (lines or {}).items():
            if not isinstance(l, dict):
                continue
            n = int(num)
            pos = l.get("Position")
            if pos is not None:
                try:
                    self.positions[n] = int(pos)
                except (TypeError, ValueError):
                    pass
            # The 2026 feed calls the current lap "NumberOfLaps"; older seasons
            # used "LapNumber". Verified by scanning the Baku archive: NumberOfLaps
            # appears 976 times, LapNumber never appears at all.
            lapno = l.get("LapNumber")
            if lapno is None:
                lapno = l.get("NumberOfLaps")
            if lapno is not None:
                try:
                    self.lap[n] = int(lapno)
                except (TypeError, ValueError):
                    pass
            # F1 also reports the pit-stop count directly, which is better than
            # counting stints: it only moves on an actual stop.
            npit = l.get("NumberOfPitStops")
            if npit is not None:
                try:
                    count = int(npit)
                except (TypeError, ValueError):
                    count = None
                if count is not None:
                    was = self.pit_stops.get(n, 0)
                    self.pit_stops[n] = count
                    for _ in range(count - was):
                        self._record_pit_stop(n, count)
            g = l.get("GapToLeader")
            if g is not None:
                self.gap[n] = g
            lt = lap_seconds(l.get("LastLapTime"))
            if lt:
                self.last_lap_time[n] = lt
                prev = self.best_lap.get(n)
                if not prev or lt < prev["time"]:
                    self.best_lap[n] = {"time": lt, "lap": lapno}
            bl = lap_seconds(l.get("BestLapTime"))
            if bl:
                cur = self.best_lap.get(n)
                if not cur or bl < cur["time"]:
                    self.best_lap[n] = {"time": bl, "lap": lapno}
            tyre = (l.get("Tyres") or {}).get("0") if isinstance(l.get("Tyres"), dict) else None
            if tyre:
                self.tyre[n] = tyre

    def _on_timing_app(self, data):
        """TimingAppData is the only topic carrying the stint table.

        TimingData has no `Stints` field at all — verified against the 2026 Baku
        archive: the string does not occur once in 59 712 stream lines and is
        absent from the final TimingData.json. FastF1's `laps['Stint']` comes
        from here too (fastf1/_api.py reads Lines[driver]['Stints']).

        Stream shape: {"Lines": {"63": {"Stints": {"0": {...}, "2": {...}}}}}
        The key is the stint index, so {"0":..,"2":..} means three stints so far.
        The number is 1-based and matches the `pit_counts` the frontend builds
        from OpenF1: 1 means still on the first set.
        """
        lines = data.get("Lines") if isinstance(data, dict) else None
        for num, l in (lines or {}).items():
            if not isinstance(l, dict):
                continue
            n = int(num)
            stints = l.get("Stints")
            count = _stint_count(stints)
            # Messages are incremental, so a smaller value later must not undo
            # a stint the driver already completed.
            if count is not None:
                self.stint[n] = max(self.stint.get(n, 0), count)
            # The newest stint also says which tyre is on the car, which is more
            # dependable than TimingData's Tyres channel.
            comp = _newest_stint(stints).get("Compound")
            if comp and str(comp).upper() != "UNKNOWN":
                self.tyre[n] = str(comp).upper()

    def _on_weather(self, data):
        for e in _entries(data):
            if not isinstance(e, dict):
                continue
            self.weather = {
                "t": e.get("Utc"),
                "air": _num(e.get("AirTemp")),
                "track": _num(e.get("TrackTemp")),
                "humidity": _num(e.get("Humidity")),
                "wind_speed": _num(e.get("WindSpeed")),
                "wind_dir": _num(e.get("WindDirection")),
                "rainfall": _num(e.get("Rainfall")),
                "pressure": _num(e.get("Pressure")),
            }
            if e.get("Utc"):
                self.updated = e.get("Utc")

    def _on_track(self, data):
        for e in _entries(data):
            if not isinstance(e, dict):
                continue
            self.track_status = e.get("Status")
            if e.get("Utc"):
                self.updated = e.get("Utc")

    def _on_lapcount(self, data):
        for e in _entries(data):
            if not isinstance(e, dict):
                continue
            self.lap_count = {"current": e.get("CurrentLap"), "total": e.get("TotalLaps")}

    def _on_team_radio(self, data):
        """Team radio, for the "Радио команды" panel.

        The topic is keyed "Captures", not "Messages" like RaceControlMessages —
        one entry per line:
            {"Captures": [{"Utc": "...", "RacingNumber": "6",
                           "Path": "TeamRadio/HAD_6_20260926_141221.mp3"}]}

        Only the path is stored. The playable URL needs SessionInfo.Path, which
        on a live feed regularly arrives *after* the first few radio messages, so
        the URL is built in snapshot() instead.
        """
        caps = data.get("Captures") if isinstance(data, dict) else None
        for c in _as_list(caps):
            if not isinstance(c, dict):
                continue
            path = c.get("Path")
            if not path:
                continue
            try:
                num = int(c.get("RacingNumber"))
            except (TypeError, ValueError):
                continue
            self.radio.append({"t": c.get("Utc"), "n": num, "path": str(path)})
        # Baku 2026 had 31 captures in a race. A cap keeps memory flat and is far
        # above what the panel shows anyway.
        self.radio = self.radio[-60:]

    def _on_rc(self, data):
        for e in _as_list(data.get("Messages") if isinstance(data, dict) else None):
            if not isinstance(e, dict):
                continue
            self.race_control.append({
                "t": e.get("Utc"),
                "msg": e.get("Message"),
                "cat": e.get("Category"),
                "flag": e.get("Flag"),
                "lap": e.get("Lap"),
            })
        self.race_control = self.race_control[-15:]
        if self.race_control:
            self.updated = self.race_control[-1].get("t")

    def _on_drivers(self, data):
        # DriverList arrives as a bare dict keyed by driver number:
        #   {"63": {"FullName": "George RUSSELL", "Tla": "RUS", ...}, ...}
        # with no "Entries" wrapper, so it needs its own handling.
        if not isinstance(data, dict):
            return
        if "Number" in data or "FullName" in data:
            items = [(data, data.get("Number") or data.get("RacingNumber"))]
        else:
            items = [(v, k) for k, v in data.items() if isinstance(v, dict)]
        for e, key in items:
            try:
                n = int(e.get("Number") or e.get("RacingNumber") or key)
            except (TypeError, ValueError):
                continue
            line = e.get("Line") or {}
            full = e.get("FullName") or e.get("BroadcastName") or ""
            parts = full.split()
            self.drivers[n] = {
                "n": n,
                "code": e.get("Tla") or (line.get("ShortName") if isinstance(line, dict) else "") or "",
                "name": " ".join(p if i == 0 else p.capitalize()
                                 for i, p in enumerate(parts)) or full,
                "short": full,
                "team": e.get("TeamName") or "",
                "team_color": "#" + str(e.get("TeamColour") or "666666").lstrip("#"),
                "color": "#" + str(e.get("Colour") or e.get("TeamColour") or "666666").lstrip("#"),
            }

    def _on_session_info(self, data):
        """Read the session identity.

        SessionInfo is a bare object with no wrapper — verified against the 2026
        Baku archive, where the payload is
            {"Meeting": {...}, "Key": 11377, "Type": "Race", "Name": "Race",
             "StartDate": ..., "Path": "2026/.../2026-09-26_Race/", ...}
        Some variants nest the same data under "Entries", and an older shape puts
        the key inside "Gaming". All three are accepted.

        This matters more than it looks: `Key` is the session key OpenF1 uses
        (so circuit_key can be resolved), and `Path` is what turns a team-radio
        capture path into a playable audio URL.
        """
        if not isinstance(data, dict):
            return
        entries = _as_list(data.get("Entries"))
        if not entries and ("Key" in data or "Gaming" in data or "Path" in data):
            entries = [data]
        for e in entries:
            if not isinstance(e, dict):
                continue
            gi = e.get("Gaming") or {}
            meeting = e.get("Meeting") or gi.get("Meeting") or {}
            key = e.get("Key") or gi.get("Key")
            name = e.get("Name") or gi.get("Name")
            path = e.get("Path")
            if key is None and path is None and name is None:
                continue
            self.session_info = {
                "meeting": (meeting or {}).get("Key") or gi.get("Meeting"),
                "session": e.get("Session") or gi.get("Session"),
                "key": key,
                "name": name,
                "type": e.get("Type"),
                "status": e.get("SessionStatus"),
                "path": path,
                "circuit_key": (meeting or {}).get("Circuit", {}).get("Key"),
            }
            if e.get("StartDate"):
                self.updated = e["StartDate"]

    # ── snapshot in the API shape the frontend already consumes ───────────
    def snapshot(self, *, circuit_key=None, is_live=False, extra_drivers=None):
        # Position.z also carries safety/medical cars (numbers 241-243 on a
        # typical weekend). DriverList is the roster of actual drivers, so it
        # is used to filter them out.
        roster = set(self.drivers)

        cars = []
        for n, c in sorted(self.cars.items()):
            if roster and n not in roster:
                continue
            if c.get("x") is None or c.get("y") is None:
                continue
            cars.append({
                "n": n,
                "x": round(c["x"], 1),
                "y": round(c["y"], 1),
                "z": c.get("z"),
                "status": c.get("status"),
                "t": c.get("t"),
                "speed": c.get("speed"),
                "throttle": c.get("throttle"),
                "brake": c.get("brake"),
                "drs": None,
                "gear": c.get("gear"),
            })

        drivers = sorted(self.drivers.values(), key=lambda d: d["n"])
        if extra_drivers:
            known = {d["n"] for d in drivers}
            drivers = drivers + [d for d in extra_drivers if d["n"] not in known]

        return {
            "generated": self.updated,
            "session_key": self.session_info.get("key"),
            "session_name": self.session_info.get("name"),
            "circuit_key": circuit_key,
            "is_live": is_live,
            "track_status": self.track_status,
            "lap_count": self.lap_count,
            "drivers": drivers,
            "cars": cars,
            "positions": dict(self.positions),
            "laps": dict(self.lap),
            "gaps": dict(self.gap),
            "pit": list(self.pit),
            "pit_stops": dict(self.pit_stops),
            "best_laps": dict(self.best_lap),
            "tyres": dict(self.tyre),
            "stints": dict(self.stint),
            "weather": self.weather,
            "race_control": list(reversed(self.race_control)),
            "radio": self._radio_for_frontend(),
        }

    def _radio_for_frontend(self) -> list:
        """Turn stored radio paths into playable URLs.

        Captures arrive with a path relative to the session directory
        ("TeamRadio/XXX_6_20260926_141221.mp3"); SessionInfo.Path supplies the
        rest. Until that arrives the entries are dropped rather than returned
        with a broken src, which would make the audio player show an error.
        """
        base = self.session_info.get("path")
        if not base:
            return []
        base = str(base).strip("/") + "/"
        out = []
        for r in self.radio:
            out.append({
                "t": r["t"],
                "n": r["n"],
                "url": "https://livetiming.formula1.com/static/" + base + r["path"],
            })
        return list(reversed(out))