"""
F1 Live Timing Service

Real-time Formula 1 data for the f1-calendar Live View tab.

Reads F1's official live timing feed through FastF1 and exposes a small JSON
API with permissive CORS, so a static GitHub Pages site can poll it from the
browser.

Why this exists
---------------
F1 publishes its live timing feed openly (SignalR on livetiming.formula1.com,
no key required), but sends no `Access-Control-Allow-Origin` header, so a
browser cannot read it directly. GitHub Pages and GitHub Actions are static and
cannot hold a long-running process. This service is the thin proxy in between.

Verified against FastF1 3.8.3:
  * car positions live in `session.pos_data` (dict driver -> DataFrame with
    X/Y/Z), NOT in `session.car_data` — car_data only has RPM/Speed/nGear/
    Throttle/Brake/DRS
  * `session.drivers` is a list of dataclasses, not a DataFrame
  * there is no `session.date_start` / `session.session_key`
  * `position_data` is incomplete for some drivers (logged warnings)

Data is for non-commercial fan use only.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# Uptime reference for /health. On a host that spins idle instances down, a
# reset of this value proves the container was stopped and restarted; a value
# that keeps growing means it never slept.
_PROCESS_STARTED = time.time()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from livews import LiveFeed

# FastF1 is imported lazily, never at module level.
#
# It drags in pandas, scipy, matplotlib, cryptography and rapidfuzz — roughly
# 400 MB of image and several seconds of start-up. The default live-only mode
# never touches any of that: it just holds a WebSocket and keeps the last value
# per driver. Lazy import keeps the deployed image at ~120 MB and the boot fast.
_ARCHIVE = None


def fastf1_deps():
    """Import and cache FastF1 + pandas. Only the archive path needs them."""
    global _ARCHIVE
    if _ARCHIVE is None:
        import fastf1
        import pandas as pd

        if ARCHIVE_FALLBACK:
            from fastf1 import Cache

            try:
                os.makedirs(CACHE_DIR, exist_ok=True)
                Cache.enable_cache(CACHE_DIR)
            except Exception as exc:      # кэш не обязателен
                log(f"кэш FastF1 недоступен ({exc}) — работаю без него")
        _ARCHIVE = (fastf1, pd)
    return _ARCHIVE

CACHE_DIR = os.environ.get("F1_CACHE_DIR", ".ffcache")
POLL_SECONDS = float(os.environ.get("F1_POLL_SECONDS", "3"))
GRID = 1000  # must match the quantisation used by data/live/circuits/*.json

# The archive path (FastF1 Session.load) needs ~780 MB, which does not fit a
# 512 MB free tier. It is therefore opt-in: with it off the service is a pure
# live feed that only reports a session while it is actually running. History is
# already covered by the precomputed OpenF1 data on the site.
ARCHIVE_FALLBACK = os.environ.get("F1_ARCHIVE_FALLBACK", "0").lower() in ("1", "true", "yes")

# Session length fallback when the schedule has no reliable end time
SESSION_LENGTH = timedelta(hours=2.5)
# Only consider sessions that started within this window
RELEVANT_WINDOW = timedelta(hours=6)

os.makedirs(CACHE_DIR, exist_ok=True)   # только для archive-режима

app = FastAPI(
    title="F1 Live Timing Service",
    version="1.0.0",
    description="Real-time F1 telemetry for the f1-calendar Live View tab.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# The snapshot is ~10 KB of JSON and the frontend polls it every 3 seconds, so
# a single viewer of a two-hour race pulls roughly 0.6 GB. Both hosts bill
# outbound traffic at $0.05/GB, and JSON of this shape compresses about 7-10x.
# Gzip is therefore not a micro-optimisation: it cuts the egress bill by nearly
# an order of magnitude and makes the tab noticeably faster.
app.add_middleware(GZipMiddleware, minimum_size=500)

_state: dict[str, Any] = {
    "session": None,
    "session_key": None,
    "snapshot": None,
    "updated": None,
    "error": None,
    "is_live": False,
    "started_at": None,
    "load_seconds": None,
    "bounds": None,
    "source": None,          # "livefeed" or "archive"
}

# Live WebSocket feed. While a session is running this is the cheap path:
# no telemetry download, no DataFrames, tens of MB of RAM.
# Created after log() is defined below.


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log(msg: str) -> None:
    print(f"[{now():%H:%M:%S}] {msg}", flush=True)


feed = LiveFeed(log=log)


def _elapsed_seconds(v):
    """Seconds in a FastF1 elapsed-time value, without importing pandas.

    The Time column of `weather_data` holds a pandas.Timedelta, which
    float() rejects. Timedelta exposes total_seconds() directly, so try that
    first and fall back to a plain number.
    """
    for attr in ("total_seconds",):
        fn = getattr(v, attr, None)
        if callable(fn):
            try:
                return float(fn())
            except (TypeError, ValueError):
                return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean(v):
    """JSON-safe scalar: turns NaN/None/odd types into None or a number.

    Deliberately free of pandas: `pd.NaT` is recognised by its type name and
    `pd.Timestamp` is a `datetime` subclass, so no import is needed.
    """
    if v is None:
        return None
    try:
        if v != v or type(v).__name__ == "NaTType":   # float('nan') / pd.NaT
            return None
    except (TypeError, ValueError):
        return None
    if isinstance(v, datetime):            # pd.Timestamp is a datetime subclass
        return v.isoformat()
    if isinstance(v, (int,)):
        return v
    try:
        f = float(v)
        if f != f:
            return None
        return round(f, 2)
    except (TypeError, ValueError):
        return str(v)


# ── session discovery ──────────────────────────────────────────────────────
def _session_start(s) -> datetime | None:
    """Naive UTC start of a FastF1 session (archive path only)."""
    for attr in ("date", "date_start"):
        v = getattr(s, attr, None)
        if v is None:
            continue
        if isinstance(v, datetime):
            return v.replace(tzinfo=None)
        try:                       # ISO-строка, если FastF1 отдаёт её как текст
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")
                                          ).replace(tzinfo=None)
        except ValueError:
            continue
    try:
        return s.session_start_time.replace(tzinfo=None)
    except Exception:
        return None


def _session_key(s) -> str | None:
    # session_info is only populated after load(); before that FastF1 raises
    # DataNotLoadedError, so this must never be called on a fresh Session.
    try:
        si = getattr(s, "session_info", None)
    except Exception:
        return None
    if isinstance(si, dict):
        # FastF1 3.x exposes the key as session_info['Key']
        for k in ("Key", "SessionKey", "session_key"):
            if si.get(k) is not None:
                return str(si[k])
    return None


def _session_ident(s) -> tuple:
    """Identity available *before* load() — event + session name."""
    ev = getattr(s, "event", None)
    return (
        getattr(ev, "EventName", None),
        getattr(s, "name", None),
        _session_start(s),
    )


SESSION_CODES = ("R", "Q", "SQ", "S", "P3", "P2", "P1")
LOOKBACK_DAYS = 10


def find_current_session():
    """Newest session that has not finished yet, preferring one that is running.

    `fastf1.get_events_remaining()` returns an EventSchedule, which is a pandas
    DataFrame subclass — iterating it yields column names, and `if not schedule`
    raises "truth value is ambiguous". Work with the DataFrame properly and
    resolve Event objects by name.
    """
    fastf1, _pd = fastf1_deps()
    year = now().year
    try:
        sched = fastf1.get_event_schedule(year, include_testing=False)
    except Exception as exc:
        raise HTTPException(503, f"F1 schedule unavailable: {exc}") from exc

    if sched is None or sched.empty or "EventDate" not in sched.columns:
        raise HTTPException(503, f"empty F1 schedule for {year}")

    ref = now()
    # Look around "now", not from the start of the season: the season schedule
    # always contains future events, so taking tail() blindly would only ever
    # see December rounds when today is October.
    lo, hi = ref - timedelta(days=LOOKBACK_DAYS), ref + timedelta(days=1)
    cand = sched[(sched["EventDate"] >= lo) & (sched["EventDate"] <= hi)]
    if cand.empty:
        # mid-season lull: fall back to the most recent completed events
        cand = sched[sched["EventDate"] <= ref].tail(2)
    if cand.empty:
        raise HTTPException(503, "no F1 events near the current date")

    recent = cand.sort_values("EventDate").tail(3)
    best = None

    for _, row in recent.iterrows():
        name = row.get("EventName")
        if not name:
            continue
        try:
            # signature is get_event(year, gp) — year is positional-first
            ev = fastf1.get_event(year, str(name))
        except Exception:
            continue
        for code in SESSION_CODES:
            try:
                s = ev.get_session(code)
            except Exception:
                continue
            start = _session_start(s)
            if start is None or start > ref + timedelta(minutes=30):
                continue
            if start <= ref < start + SESSION_LENGTH:
                return s, True              # running right now
            if best is None or start > best[0]:
                best = (start, s)

    if best is None:
        raise HTTPException(503, "no active or recent session")
    return best[1], False


# ── snapshot ───────────────────────────────────────────────────────────────
def build_snapshot(session, bounds) -> dict:
    """Build the snapshot.

    Coordinates are sent RAW, not quantised. The consumer quantises them using
    the bounds stored in data/live/circuits/{circuit_key}.json, which are padded
    and cover the whole circuit. Deriving bounds from live telemetry instead
    would be wrong: early in a session only part of the track has been driven,
    so the live bounding box is much tighter than the real circuit and the cars
    would be drawn in the wrong places.
    """
    x0, y0, x1, y1 = bounds

    cars = []
    pos = getattr(session, "pos_data", None)
    if isinstance(pos, dict):
        for num, df in pos.items():
            if df is None or df.empty or "X" not in df.columns:
                continue
            row = df.dropna(subset=["X", "Y"]).iloc[-1]
            cars.append({
                "n": int(num),
                "x": round(float(row["X"]), 1),
                "y": round(float(row["Y"]), 1),
                "t": str(row.get("Date")),
                "status": clean(row.get("Status")),
            })

    # speed/throttle/DRS come from car_data, keyed by the same driver numbers
    telemetry = {}
    car = getattr(session, "car_data", None)
    if isinstance(car, dict):
        for num, df in car.items():
            if df is None or df.empty:
                continue
            r = df.iloc[-1]
            telemetry[int(num)] = {
                "speed": clean(r.get("Speed")),
                "throttle": clean(r.get("Throttle")),
                "brake": clean(r.get("Brake")),
                "drs": clean(r.get("DRS")),
                "gear": clean(r.get("nGear")),
            }
    for c in cars:
        c.update(telemetry.get(c["n"], {}))

    positions, gaps, best, tyres, stints = {}, {}, {}, {}, {}

    # FastF1 exposes no gap column: `laps` has none and `results` has none either
    # (verified on 3.8.3). Gaps to the leader live only in the raw timing feed,
    # which FastF1 does not surface as a public attribute, so gaps stay empty
    # unless something provides them. The client falls back gracefully.
    final_gaps = {}
    res = getattr(session, "results", None)
    if res is not None and len(res) and "GapToLeader" in getattr(res, "columns", []):
        for row in res.itertuples():
            num = clean(getattr(row, "DriverNumber", None))
            g = clean(getattr(row, "GapToLeader", None))
            if num is not None and g is not None and not str(g).startswith("+"):
                final_gaps[int(num)] = g

    laps = getattr(session, "laps", None)
    if laps is not None and not laps.empty:
        for num, grp in laps.groupby("DriverNumber"):
            n = int(num)
            last = grp.iloc[-1]
            p = clean(last.get("Position"))
            if p is not None:
                positions[n] = int(p)
            g = last.get("GapToLeader")
            if g is None:
                # `laps` has no GapToLeader column; take it from the classification
                g = final_gaps.get(n)
            if g is not None:
                text = str(g)
                if not text.startswith("+"):
                    try:
                        gaps[n] = round(float(text), 3)
                    except ValueError:
                        gaps[n] = text
            done = grp[grp["LapTime"].notna()] if "LapTime" in grp.columns else grp.iloc[:0]
            if len(done):
                bt = done.loc[done["LapTime"].idxmin()]
                best[n] = {
                    "lap": int(clean(bt.get("LapNumber")) or 0),
                    "time": seconds(bt.get("LapTime")),
                    "compound": clean(bt.get("Compound")),
                }
            if "Compound" in grp.columns and grp["Compound"].notna().any():
                tyres[n] = str(grp["Compound"].dropna().iloc[-1])
            if "Stint" in grp.columns and grp["Stint"].notna().any():
                stints[n] = int(grp["Stint"].dropna().iloc[-1])

    weather = {}
    wdf = getattr(session, "weather_data", None)
    if wdf is not None and not wdf.empty:
        last = wdf.iloc[-1]
        # weather_data has a RangeIndex and a *timedelta* Time column, so the
        # absolute timestamp has to be rebuilt from the session start.
        stamp = None
        elapsed = last.get("Time")
        base = _session_start(session)
        if elapsed is not None and base is not None:
            secs = _elapsed_seconds(elapsed)
            if secs is not None:
                stamp = (base + timedelta(seconds=secs)).isoformat()
        weather = {
            "t": stamp,
            "air": clean(last.get("AirTemp")),
            "track": clean(last.get("TrackTemp")),
            "humidity": clean(last.get("Humidity")),
            "wind_speed": clean(last.get("WindSpeed")),
            "wind_dir": clean(last.get("WindDirection")),
            "rainfall": clean(last.get("Rainfall")),
            "pressure": clean(last.get("Pressure")),
        }

    rc = []
    msgs = getattr(session, "race_control_messages", None)
    if msgs is not None and not msgs.empty:
        for m in msgs.tail(15).itertuples():
            rc.append({
                "t": clean(getattr(m, "Time", None)),
                "msg": clean(getattr(m, "Message", None)) or clean(getattr(m, "Category", None)),
                "cat": clean(getattr(m, "Category", None)),
                "flag": clean(getattr(m, "Flag", None)),
                "lap": clean(getattr(m, "Lap", None)),
            })

    track_status = None
    ts = getattr(session, "track_status", None)
    if ts is not None and not ts.empty:
        raw = ts.iloc[-1].get("Status")
        # Status is a numeric code; keep it verbatim rather than letting clean()
        # turn 1 into 1.0, but expose a readable label too.
        track_status = track_status_label(raw)

    # FastF1's session_info does not carry driver metadata (names, teams,
    # colours), and Event has no .drivers in 3.8.3. OpenF1's free endpoint
    # provides it and is keyed by the very same session_key
    # (session_info['Key'] == OpenF1 session_key), so one cheap call covers it.
    circuit_key = None
    meeting = None
    si = getattr(session, "session_info", None)
    if isinstance(si, dict):
        meeting = si.get("Meeting")
    if isinstance(meeting, dict):
        circuit = meeting.get("Circuit")
        if isinstance(circuit, dict):
            circuit_key = clean(circuit.get("Key"))

    drivers = []
    sk = _session_key(session)
    if sk:
        for d in fetch_driver_roster(sk):
            drivers.append(d)

    return {
        "generated": now().isoformat(),
        "session_key": _session_key(session),
        "session_name": getattr(session, "name", None),
        "date_start": _session_start(session).isoformat() if _session_start(session) else None,
        "event": getattr(getattr(session, "event", None), "EventName", None),
        "circuit_key": circuit_key,
        "bounds": [round(v, 1) for v in bounds],
        "grid": GRID,
        "is_live": bool(_state["is_live"]),
        "track_status": track_status,
        "drivers": drivers,
        "cars": cars,
        "positions": positions,
        "gaps": gaps,
        "best_laps": best,
        "tyres": tyres,
        "stints": stints,
        "weather": weather,
        "race_control": rc,
    }


# ── driver roster ──────────────────────────────────────────────────────────
_roster_cache: dict[str, list[dict]] = {}


def fetch_driver_roster(session_key: str) -> list[dict]:
    """Names, teams and colours for one session, from OpenF1 (free, no key).

    FastF1 3.8.3 does not expose driver metadata: `session_info` has no driver
    list and `Event` has no `.drivers`. OpenF1's `drivers` endpoint is keyed by
    the same session_key, so one cached request per session is enough.
    """
    if session_key in _roster_cache:
        return _roster_cache[session_key]

    import json
    import urllib.request

    url = f"https://api.openf1.org/v1/drivers?session_key={session_key}"
    req = urllib.request.Request(url, headers={"User-Agent": "f1-live-service/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:
        log(f"driver roster unavailable for {session_key}: {exc}")
        return []

    out = []
    for r in sorted(rows, key=lambda d: d.get("driver_number", 0)):
        full = (r.get("full_name") or "").split()
        name = " ".join(p if i == 0 else p.capitalize() for i, p in enumerate(full))
        out.append({
            "n": r.get("driver_number"),
            "code": (full[-1][:3].upper() if len(full) > 1 else (full[0][:3].upper() if full else "")),
            "name": name or f"#{r.get('driver_number')}",
            "team": r.get("team_name") or "",
            "team_color": "#" + str(r.get("team_colour") or "666666").lstrip("#"),
            "color": "#" + str(r.get("colour") or r.get("team_colour") or "666666").lstrip("#"),
        })
    _roster_cache[session_key] = out
    return out


def seconds(v) -> float | None:
    """FastF1 lap times arrive as pandas Timedelta objects; JSON can't hold those."""
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except (TypeError, ValueError):
        return None
    total = _elapsed_seconds(v)
    return round(total, 3) if total is not None else None


TRACK_STATUS = {
    "1": "жёлтые флаги нет",
    "2": "жёлтый флаг",
    "3": "трасса закрыта",
    "4": "сейфти-кар",
    "5": "красный флаг",
    "6": "виртуальный сейфти-кар",
    "7": "VCAS заканчивается",
    "8": "трасса открыта",
}


def track_status_label(raw) -> str | None:
    if raw is None:
        return None
    key = str(raw).strip()
    return TRACK_STATUS.get(key, key)


_circuit_cache: dict[str, str] = {}


def _circuit_key_for(session_key: str) -> str | None:
    """Map a session key to OpenF1's circuit_key.

    F1's SessionInfo.Gaming.Key and OpenF1's session_key are the same number
    space, so one cached OpenF1 call per session is enough.
    """
    if session_key in _circuit_cache:
        return _circuit_cache[session_key]
    import json as _json
    import urllib.request as _u
    url = f"https://api.openf1.org/v1/sessions?session_key={session_key}"
    try:
        req = _u.Request(url, headers={"User-Agent": "f1-live-service/1.1"})
        with _u.urlopen(req, timeout=15) as r:
            rows = _json.loads(r.read().decode())
    except Exception:
        return None
    if not rows:
        return None
    ck = str(rows[0].get("circuit_key"))
    _circuit_cache[session_key] = ck
    return ck


def _snapshot_or_503() -> dict:
    if _state["snapshot"] is None:
        raise HTTPException(503, _state["error"] or "service is still warming up")
    return _state["snapshot"]


# ── poller ─────────────────────────────────────────────────────────────────
def _waiting_snapshot() -> dict:
    """Заглушка для периодов между сессиями.

    Формат совпадает с боевым снимком, чтобы фронтенд не различал ветки:
    пустые списки вместо null, флаг waiting_for_session.
    """
    return {
        "generated": now().isoformat(),
        "is_live": False,
        "waiting_for_session": True,
        "message": "Ожидание активной сессии F1 — данные появятся здесь "
                   "во время трансляции. Если карточка не обновилась сразу, "
                   "это разгон сервиса: он засыпает между сессиями ради нулевой "
                   "стоимости, обычно 10–15 секунд.",
        "feed": feed.status(),
        "drivers": [], "cars": [], "positions": {}, "laps": {}, "gaps": {},
        "best_laps": {}, "tyres": {}, "stints": {},
        "weather": {}, "race_control": [], "radio": [],
    }


async def poll_loop():
    loaded_ident = None
    was_live = False
    while True:
        try:
            # --- 1. живой путь: сокет передаёт данные прямо сейчас ---
            st = feed.status()
            fresh = (st["connected"] and st["messages"] > 0
                     and (st["idle_seconds"] or 999) < 90)
            if fresh and feed.state.cars:
                sk = feed.state.session_info.get("key")
                # SessionInfo carries Meeting.Circuit.Key directly, which is the
                # same number space OpenF1 uses for circuit_key. Only fall back
                # to the OpenF1 lookup when that field is missing.
                circuit_key = feed.state.session_info.get("circuit_key")
                if circuit_key is None and sk:
                    circuit_key = _circuit_key_for(str(sk))
                snap = feed.state.snapshot(circuit_key=circuit_key, is_live=True)
                _state["session_key"] = str(sk) if sk else _state["session_key"]
                _state["snapshot"] = snap
                _state["updated"] = now().isoformat()
                _state["is_live"] = True
                _state["source"] = "livefeed"
                _state["error"] = None
                if not was_live:
                    log(f"живая сессия: key={_state['session_key']} "
                        f"машин={len(feed.state.cars)}")
                    was_live = True
                await asyncio.sleep(POLL_SECONDS)
                continue

            # --- 2. запасной путь: последняя завершённая сессия через load() ---
            # Выключен по умолчанию: требует ~780 МБ RAM. История и так есть
            # в статических данных сайта.
            if not ARCHIVE_FALLBACK:
                _state["source"] = "livefeed"
                _state["is_live"] = False
                # Снимок живой сессии после финиша застывает: машины стоят на
                # последнем круге, но с каждым опросом выглядят всё свежее. Как
                # только поток перестал идти, отдаём заглушку — иначе фронтенд
                # будет показывать мёртвые координаты.
                if was_live:
                    log("сессия больше не идёт — снимок сброшен")
                    _state["snapshot"] = None
                    _state["session_key"] = None
                    was_live = False
                if _state["snapshot"] is None:
                    _state["error"] = None
                    _state["updated"] = now().isoformat()
                    _state["snapshot"] = _waiting_snapshot()
                else:
                    # Заглушка создаётся один раз, а сокет переподключается сам,
                    # поэтому его статус нужно освежать — иначе в ответе остаётся
                    # connected: false, хотя связь есть.
                    _state["snapshot"]["feed"] = feed.status()
                await asyncio.sleep(POLL_SECONDS)
                continue

            session, is_live = await asyncio.to_thread(find_current_session)
            ident = _session_ident(session)

            # find_current_session() builds a fresh Session object every call, so
            # identity must be compared by event/name, not by object or by
            # session_info (unavailable before load).
            if ident != loaded_ident:
                log(f"session {ident[0]} / {ident[1]} live={is_live} — loading "
                    f"(first load is slow)")
                t0 = time.time()
                # FastF1 3.8.3 signature: load(*, laps, telemetry, weather,
                # messages, livedata) — no telemetry_distance argument.
                await asyncio.to_thread(
                    session.load,
                    telemetry=True,
                    laps=True,
                    weather=True,
                    messages=True,
                )
                pos = getattr(session, "pos_data", None) or {}
                xs, ys = [], []
                for df in pos.values():
                    if df is not None and not df.empty and "X" in df.columns:
                        d = df.dropna(subset=["X", "Y"])
                        if not d.empty:
                            xs.append((float(d.X.min()), float(d.X.max())))
                            ys.append((float(d.Y.min()), float(d.Y.max())))
                if not xs:
                    raise RuntimeError("no position telemetry returned by the feed")
                bounds = (min(a for a, _ in xs), min(a for a, _ in ys),
                          max(b for _, b in xs), max(b for _, b in ys))
                _state["bounds"] = bounds
                loaded_ident = ident
                _state["session"] = session
                _state["session_key"] = _session_key(session)
                _state["started_at"] = now()
                _state["load_seconds"] = round(time.time() - t0, 1)
                log(f"loaded key={_state['session_key']} in "
                    f"{_state['load_seconds']}s, X[{bounds[0]:.0f},{bounds[2]:.0f}] "
                    f"Y[{bounds[1]:.0f},{bounds[3]:.0f}]")
            else:
                # find_current_session() hands back a *fresh* Session object every
                # poll. It carries no data, so keep using the loaded instance.
                session = _state["session"]

            _state["is_live"] = is_live
            _state["source"] = "archive"
            snap = await asyncio.to_thread(build_snapshot, session, _state["bounds"])
            _state["snapshot"] = snap
            _state["updated"] = snap["generated"]
            _state["error"] = None

        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"
            if msg != _state["error"]:
                log(f"poll error: {msg}")
            _state["error"] = msg

        await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def startup():
    log(f"cache={CACHE_DIR} poll={POLL_SECONDS}s "
        f"archive_fallback={'on' if ARCHIVE_FALLBACK else 'off'}")
    feed.start()
    asyncio.create_task(poll_loop())


# ── endpoints ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    age = None
    if _state["updated"]:
        age = round((now() - datetime.fromisoformat(_state["updated"])).total_seconds(), 1)
    return {
        "ok": _state["error"] is None and _state["snapshot"] is not None,
        "error": _state["error"],
        "session_key": _state["session_key"],
        "snapshot_age_seconds": age,
        "load_seconds": _state["load_seconds"],
        "is_live": _state["is_live"],
        "source": _state["source"],
        "archive_fallback": ARCHIVE_FALLBACK,
        "feed": feed.status(),
        "grid": GRID,
        "polling": POLL_SECONDS,
        # Seconds since this process started. On a host that spins idle
        # instances down, a reset here proves the container was stopped and
        # restarted; a value that keeps growing means it never slept.
        "uptime_seconds": int(time.time() - _PROCESS_STARTED),
    }


@app.get("/api/status")
async def status():
    snap = _state["snapshot"] or {}
    return {
        "ok": _state["error"] is None and snap.get("generated") is not None,
        "error": _state["error"],
        "session_key": snap.get("session_key"),
        "session_name": snap.get("session_name"),
        "is_live": snap.get("is_live"),
        "updated": snap.get("generated"),
    }


@app.get("/api/snapshot")
@app.get("/api/session")
async def snapshot():
    return _snapshot_or_503()


@app.get("/api/session/{session_key}")
async def snapshot_by_key(session_key: str):
    snap = _state["snapshot"] or {}
    if str(session_key) == str(_state["session_key"]):
        return snap
    raise HTTPException(
        404,
        "only the currently tracked session is available live; "
        "use the precomputed static data for past sessions",
    )


@app.get("/")
async def root():
    return {
        "service": "F1 Live Timing Service",
        "docs": "/docs",
        "endpoints": ["/health", "/api/status", "/api/snapshot"],
        "source": "F1 live timing WebSocket feed (no API key, non-commercial)",
        "archive_fallback": ARCHIVE_FALLBACK,
    }