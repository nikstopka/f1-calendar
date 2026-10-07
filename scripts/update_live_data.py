#!/usr/bin/env python3
"""
F1 Calendar — Live View data builder (OpenF1)

Pulls session telemetry from the free OpenF1 API and precomputes compact
static JSON for the Live View tab. Everything is committed to the repo so the
browser only ever loads plain files — no API calls, no rate limits, no CORS.

Generates:
  data/live/index.json              — every OpenF1 session since 2023
  data/live/circuits/{key}.json     — track outline derived from real telemetry
  data/live/sessions/{key}.json     — per-session detail (positions, weather, pit, radio…)

Design notes
------------
* OpenF1's free tier serves a session only ~30 min AFTER it ends, and then it
  is immutable. So every session is fetched exactly once and never refetched.
* Raw location telemetry is 3.7 Hz × 22 drivers → ~300k rows per race. We
  downsample to LIVE_POS_SAMPLE_SECONDS and quantise coordinates to integers
  on a 1000×1000 track grid, which is what keeps the files small.
* Circuit outlines are derived from telemetry itself (one driver's lap), so no
  hand-drawn track geometry is needed for any circuit.
"""

import json
import math
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
LIVE_DIR = PROJECT_DIR / "data" / "live"
CIRCUITS_DIR = LIVE_DIR / "circuits"
SESSIONS_DIR = LIVE_DIR / "sessions"
INDEX_PATH = LIVE_DIR / "index.json"

API_BASE = "https://api.openf1.org/v1"
UA = "F1-Calendar-Bot/1.1 (+github pages static site)"

# ─── Config ───
MIN_YEAR = 2023                 # OpenF1 free tier coverage starts here
POS_SAMPLE_SECONDS = 5          # keep one position frame every N seconds
WEATHER_SAMPLE_SECONDS = 300    # weather changes slowly
GRID = 1000                     # coordinate quantisation grid
CHUNK_SECONDS = 600             # API read window per request (10 min)
# Consecutive empty windows that mean the telemetry really has ended. One is
# not enough — the gaps are real.
EMPTY_WINDOW_STREAK = 3
# v2: leading frames before the field reaches the racing line are dropped.
FRAMES_VERSION = 2
OUTLINE_POINTS = 360            # resolution of the derived track outline
MAX_NEW_SESSIONS_PER_RUN = 4    # keep API usage and commit size bounded
# Circuit outlines cost one location query each, so a few per run is cheap; the
# archive starts with every track missing one.
MAX_CIRCUIT_BUILDS_PER_RUN = 6
# Sessions tried per circuit before giving up on its outline.
CIRCUIT_SESSION_TRIES = 4
DETAIL_KEEP_RECENT = 60         # how many recent sessions keep a detail file
API_PAUSE = 0.45                # stay under the 3 requests/second limit


def log(msg):
    print(f"  {msg}")


# ─── API ───
def of1_get(path, retries=3, retry_429=False):
    url = f"{API_BASE}/{path}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:120]
            if e.code == 422:          # payload too large — caller should chunk
                log(f"422 too large for {path}")
                return None
            if e.code == 404:
                # OpenF1 answers a window with no rows with 404, so this is an
                # empty result, not a failure. Returning None here made every
                # session look broken the moment its telemetry ran out: the
                # reader took None for a refused request and dropped the whole
                # session instead of stopping cleanly.
                return []
            if e.code == 429:
                # OpenF1 allows 3 requests a second and 30 a minute. A quick
                # retry makes it worse — the limit is per minute, so it has to
                # be waited out.
                #
                # Bulk reads pass retry_429: a race is dozens of windows long,
                # and failing the whole session on one refused window meant no
                # progress at all. Single exploratory reads fail fast instead.
                if retry_429:
                    log("429 — ждём и повторяем окно")
                    time.sleep(30)
                    continue
                log("429 — лимит запросов, ждём и прекращаем попытки")
                time.sleep(30)
                return None
            log(f"HTTP {e.code} ({detail}) attempt {attempt+1}")
            time.sleep(2 + attempt * 2)
        except Exception as e:
            log(f"error {e} attempt {attempt+1}")
            time.sleep(3 + attempt * 2)
    log(f"failed: {path}")
    return None


# ─── Helpers ───
def parse_dt(s):
    return datetime.fromisoformat(s) if s else None


def quantise(points, bounds):
    """Map raw x/y into integers on a GRID x GRID canvas (y flipped for SVG)."""
    x0, y0, x1, y1 = bounds
    w = max(x1 - x0, 1)
    h = max(y1 - y0, 1)
    out = []
    for x, y in points:
        qx = int(round((x - x0) / w * GRID))
        qy = int(round((y1 - y) / h * GRID))
        out.append((max(0, min(GRID, qx)), max(0, min(GRID, qy))))
    return out


def compute_bounds(points, pad=200):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def thin(points, target):
    """Reduce a point list to roughly `target` points, preserving order."""
    n = len(points)
    if n <= target:
        return points
    step = n / target
    return [points[min(int(i * step), n - 1)] for i in range(target)]


def fmt_seconds(sec):
    """OpenF1's `laps` endpoint already reports seconds (sector times sum exactly
    to lap_duration, e.g. 114.856 for a Baku opening lap) — so no conversion."""
    if sec is None:
        return None
    return round(float(sec), 3)


def hex_colour(value, fallback="#666"):
    """OpenF1 returns bare hex like 'F47600' without a leading '#'."""
    if not value:
        return fallback
    v = str(value).strip()
    return v if v.startswith("#") else "#" + v


def tidy_name(full_name):
    """'Lando NORRIS' -> 'Lando Norris' to match the rest of the site."""
    parts = (full_name or "").split()
    return " ".join(p if i == 0 else p.capitalize() for i, p in enumerate(parts))


def driver_code(full_name, number):
    """Prefer the 3-letter code; fall back to the family name from full_name."""
    parts = (full_name or "").split()
    if not parts:
        return str(number)
    last = parts[-1]
    return last[:3].upper() if len(parts) > 1 else last[:3].upper()


# ─── Circuit outline ───
def load_circuit(circuit_key):
    p = CIRCUITS_DIR / f"{circuit_key}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def build_circuit(circuit_key, session, loc_rows):
    """Derive the track outline from one driver's real trajectory.

    Two traps here, both of which shipped a broken map before:

    - OpenF1 reports (0, 0) for a car that is not on the circuit. Keeping those
      rows collapses the bounding box to a degenerate square around the origin
      and every outline point onto its centre — Monza came out as one repeated
      point, with bounds [-200, -200, 200, 200].
    - Nothing checked the result. A bad outline was written to disk and
      load_circuit() never rebuilds it, so the map stayed wrong for good.

    So the points are filtered and the outcome is validated before returning.
    """
    if not loc_rows:
        return None
    pts = [(r["x"], r["y"]) for r in loc_rows
           if r.get("x") is not None and r.get("y") is not None
           and not (r["x"] == 0 and r["y"] == 0)]
    if len(pts) < 50:
        return None

    if not outline_is_sane(pts):
        return None

    bounds = compute_bounds(pts)
    path = thin(pts, OUTLINE_POINTS)
    return {
        "circuit_key": circuit_key,
        "circuit_short_name": session.get("circuit_short_name", ""),
        "country_code": session.get("country_code", ""),
        "grid": GRID,
        "bounds": [round(b) for b in bounds],
        "outline": [list(p) for p in quantise(path, bounds)],
        "built": datetime.now(timezone.utc).isoformat(),
    }


# A real circuit outline covers a decent area and repeats few points. These are
# the thresholds that caught Monza (one unique point) and Madring (two).
MIN_UNIQUE_POINTS = 200
MIN_SPAN_M = 300
# How many drivers to try before giving up. The first one is not reliable: a
# driver who never got on track reports (0, 0) throughout.
OUTLINE_DRIVER_TRIES = 3
# The outline needs a longer window than an API chunk, and it needs to be able
# to move forward in time. At the nominal start of a session every car sits
# stationary in the pit lane: Suzuka race returned 2278 rows over the first ten
# minutes but only three distinct points, and all four drivers tried were stuck
# in the same place. Starting half an hour later gave 4180 distinct points.
#
# Two windows and three drivers is six requests per track. The earlier three by
# four ran straight into OpenF1's 30-requests-a-minute cap.
OUTLINE_WINDOW_SECONDS = 1800
OUTLINE_WINDOW_TRIES = 2


def outline_is_sane(pts) -> bool:
    """Reject trajectories that cannot be a track shape."""
    if len(set(pts)) < MIN_UNIQUE_POINTS:
        return False
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs)) > MIN_SPAN_M and (max(ys) - min(ys)) > MIN_SPAN_M


def circuit_is_usable(circuit) -> bool:
    """True when a stored outline is good enough to map positions with."""
    if not circuit:
        return False
    outline = circuit.get("outline") or []
    if len(set(map(tuple, outline))) < MIN_UNIQUE_POINTS:
        return False
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    if len(outline) > 1 and len(set(xs)) <= 1 and len(set(ys)) <= 1:
        return False
    # A span under a fifth of the canvas means the shape collapsed.
    return (max(xs) - min(xs)) > GRID * 0.2 or (max(ys) - min(ys)) > GRID * 0.2


def build_outline_from_any_driver(circuit_key, session, session_key, driver_list, scan_start):
    """Try later windows and other drivers until one yields a usable outline.

    Two reasons a first attempt fails, and they need different fixes:

    - the car never got on track, so its coordinates stay at the origin — another
      driver helps there;
    - the window is too early in the session, when every car is still stationary
      in the pits — no driver helps, only a later window does.

    So the search sweeps forward in time first and tries a driver inside each
    window, rather than cycling through drivers at one fixed moment.
    """
    for w in range(OUTLINE_WINDOW_TRIES):
        w_start = scan_start + timedelta(seconds=w * OUTLINE_WINDOW_SECONDS)
        w_end = w_start + timedelta(seconds=OUTLINE_WINDOW_SECONDS)
        for n, driver_number in enumerate(driver_list[:OUTLINE_DRIVER_TRIES]):
            if n or w:
                time.sleep(API_PAUSE)
            rows = of1_get(f"location?session_key={session_key}"
                           f"&driver_number={driver_number}"
                           f"&date%3E{w_start.isoformat()}"
                           f"&date%3C{w_end.isoformat()}")
            circuit = build_circuit(circuit_key, session, rows)
            if circuit:
                log(f"контур построен по пилоту №{driver_number} "
                    f"(окно +{w * OUTLINE_WINDOW_SECONDS}s)")
                return circuit
        log(f"окно +{w * OUTLINE_WINDOW_SECONDS}s: траектории не годятся")
    return None


# ─── Session detail ───
def fetch_locations(session_key, start, end):
    """Pull location telemetry in bounded windows to avoid the 422 size cap.

    Telemetry normally starts before the nominal date_start (Baku 2026 running
    data begins 10:07 while date_start says 11:00) and stops before date_end, so
    the caller must pass a real start time and an empty window is treated as the
    natural end of the data rather than a failure.
    """
    rows = []
    cur = start
    empty_streak = 0
    while cur < end:
        nxt = min(cur + timedelta(seconds=CHUNK_SECONDS), end)
        q = (f"location?session_key={session_key}"
             f"&date%3E{cur.isoformat()}&date%3C{nxt.isoformat()}")
        got = of1_get(q, retries=6, retry_429=True)
        if got is None:
            # A failed request is not the end of the data. Treating it as one
            # truncated a Sepang race from 2044 frames to 66: a single rate-limit
            # answer looked like an empty window, and everything after it was
            # dropped. Fail the session instead, so it is retried in full later.
            return rows, False
        if not got:
            # Telemetry is not continuous: a Monaco race had data at 13:00 and
            # again at 14:00 with a quiet half hour between. Stopping at the
            # first empty window cut the session at 16 of 120 minutes.
            empty_streak += 1
            if empty_streak >= EMPTY_WINDOW_STREAK:
                return rows, bool(rows)
            cur = nxt
            continue
        empty_streak = 0
        rows.extend(got)
        cur = nxt
        time.sleep(API_PAUSE)
    return rows, True


def build_frames(loc_rows, driver_numbers, start, end, bounds, outline=None):
    """Bucket raw 3.7 Hz samples into POS_SAMPLE_SECONDS frames.

    The frame range is taken from the actual telemetry timestamps, not from the
    nominal date_start/date_end — real running data routinely starts earlier and
    finishes earlier than the published slot (Baku 2026: 10:07→12:42 vs
    11:00→13:00), and padding with empty frames wastes ~40% of the replay.
    """
    slot = POS_SAMPLE_SECONDS
    stamps = [parse_dt(r["date"]) for r in loc_rows if r.get("date")]
    real_start = min(stamps)
    real_end = max(stamps)
    n_frames = int((real_end - real_start).total_seconds()) // slot + 1
    index = {num: i for i, num in enumerate(driver_numbers)}
    frames = [[-1] * (len(driver_numbers) * 2) for _ in range(n_frames)]
    times = [(real_start + timedelta(seconds=i * slot)) for i in range(n_frames)]

    for r in loc_rows:
        i = index.get(r.get("driver_number"))
        if i is None or r.get("x") is None:
            continue
        f = int((parse_dt(r["date"]) - real_start).total_seconds()) // slot
        if not (0 <= f < n_frames):
            continue
        x, y = quantise([(r["x"], r["y"])], bounds)[0]
        frames[f][i * 2] = x
        frames[f][i * 2 + 1] = y

    times, frames = trim_pre_session(times, frames, outline)
    return [[t.isoformat() for t in times], frames]


# Two things say the field is out on track rather than queued in the pits: it is
# near the racing line, and the cars are not all on one spot.
#
# The second condition is not optional. The pit exit is itself part of the
# racing surface, so a parked grid can sit 10 units from the line — well inside
# any sane distance threshold — with all 22 cars stacked. Distance alone kept
# those frames and the Live View opened on a pile of cars.
#
# Distance is a median in canvas units, out of GRID=1000.
PRE_SESSION_DISTANCE = 25
# Share of cars that must sit on distinct coordinates. Quantised frames mean two
# nearby cars can share a cell, so a spread field lands near 0.64, not 1.0.
PRE_SESSION_SPREAD = 0.6


def _median(xs):
    s = sorted(xs)
    return s[len(s) // 2] if s else 0.0


def trim_pre_session(times, frames, outline):
    """Drop the frames before the cars are actually out on the track.

    Telemetry starts when the session session starts, but the field does not
    reach the racing line straight away. Those opening frames are real data —
    and they are exactly what the Live View showed when it opened, with the
    whole grid piled up in one spot well away from the track, which reads as a
    broken map. Removing them costs nothing and makes the first frame honest.
    """
    if not outline or len(outline) < 3 or len(frames) < 10:
        return times, frames

    def field_state(frame):
        """(median distance to the outline, share of cars on distinct points).

        Both are needed. The pit exit is part of the racing surface, so a parked
        grid can sit only 10 units from the line and pass a distance test while
        every car is still stacked on one spot — which is how a Spa race kept
        150 opening frames showing the whole field piled up in the pits.
        """
        ds = []
        at = set()
        for k in range(0, len(frame) - 1, 2):
            if frame[k] < 0 or frame[k + 1] < 0:
                continue
            at.add((frame[k], frame[k + 1]))
            ds.append(point_segment_distance((frame[k], frame[k + 1]), outline))
        if not ds:
            return None
        return _median(ds), len(at) / len(ds)

    start = 0
    for i, frame in enumerate(frames):
        st = field_state(frame)
        if st is None:
            continue
        d, spread = st
        if d <= PRE_SESSION_DISTANCE and spread >= PRE_SESSION_SPREAD:
            start = i
            break
        start = i + 1
    else:
        return times, frames            # never on track — keep everything
    if start == 0:
        return times, frames
    dropped = start
    return times[start:], frames[start:]


def point_segment_distance(p, outline):
    """Shortest distance from p to a closed polyline."""
    best = 1e9
    px, py = p
    n = len(outline)
    for i in range(n):
        x1, y1 = outline[i]
        x2, y2 = outline[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L = dx * dx + dy * dy
        t = 0 if L == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
        best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def frame_index(times, ts):
    """Map a timestamp onto the nearest frame index (or None if outside)."""
    if not times or ts is None:
        return None
    first = parse_dt(times[0])
    i = int((ts - first).total_seconds()) // POS_SAMPLE_SECONDS
    return i if 0 <= i < len(times) else None


def build_position_events(pos_rows, driver_numbers, times):
    """OpenF1's `position` endpoint only records CHANGES, not every tick.

    Storing them sparsely as [frame, driver_index, position] keeps this tiny
    (~4 KB per session instead of ~60 KB as a dense array); the browser carries
    the last known value forward while replaying.
    """
    if not pos_rows or not times:
        return []
    index = {num: i for i, num in enumerate(driver_numbers)}
    events = []
    for r in sorted(pos_rows, key=lambda x: x.get("date") or ""):
        di = index.get(r.get("driver_number"))
        f = frame_index(times, parse_dt(r.get("date")))
        if di is None or f is None or r.get("position") is None:
            continue
        events.append([f, di, int(r["position"])])
    return events


def stint_at(stints, driver_number, lap):
    """Compound of the stint covering a given lap for a driver."""
    if not stints or lap is None:
        return None
    for s in stints:
        if s.get("driver_number") != driver_number:
            continue
        lo, hi = s.get("lap_start"), s.get("lap_end")
        if lo is None:
            continue
        if lap >= lo and (hi is None or lap <= hi):
            return s.get("compound")
    return None


def downsample(rows, seconds, key="date"):
    """Keep at most one row per `seconds` interval."""
    if not rows:
        return rows
    rows = sorted(rows, key=lambda r: r.get(key) or "")
    out, last = [], None
    for r in rows:
        d = parse_dt(r.get(key))
        if d is None:
            continue
        if last is None or (d - last).total_seconds() >= seconds:
            out.append(r)
            last = d
    return out


def build_session(session):
    sk = session["session_key"]
    start, end = parse_dt(session["date_start"]), parse_dt(session["date_end"])
    if not start or not end:
        return None

    log(f"session {sk} {session['session_name']} {session['circuit_short_name']} "
        f"{start:%Y-%m-%d %H:%M}Z")

    drv = of1_get(f"drivers?session_key={sk}")
    time.sleep(API_PAUSE)
    if not drv:
        return None

    driver_list = sorted({d["driver_number"] for d in drv})
    drivers = [{
        "n": d["driver_number"],
        "code": driver_code(d.get("full_name"), d["driver_number"]),
        "name": tidy_name(d.get("full_name")) or f"#{d['driver_number']}",
        "team": d.get("team_name") or "",
        "team_color": hex_colour(d.get("team_colour")),
        "color": hex_colour(d.get("colour") or d.get("team_colour")),
    } for d in sorted(drv, key=lambda x: x["driver_number"])]

    # --- positions first: their earliest timestamp is the real session start ---
    # `position` is keyed by meeting_key, not session_key — a common trap.
    meeting_key = drv[0].get("meeting_key") if drv else None
    positions = []
    if meeting_key is not None:
        positions = of1_get(f"position?meeting_key={meeting_key}&session_key={sk}") or []
        time.sleep(API_PAUSE)

    scan_start = start
    stamps = [parse_dt(p["date"]) for p in positions if p.get("date")]
    if stamps:
        # Running data begins well before the nominal slot; pad a little for safety.
        scan_start = min(stamps) - timedelta(minutes=2)

    # --- car coordinates (heaviest) ---
    circuit = load_circuit(session["circuit_key"])
    if circuit is None or not circuit_is_usable(circuit):
        circuit = build_outline_from_any_driver(session["circuit_key"], session, sk,
                                               driver_list, scan_start)
        if circuit:
            CIRCUITS_DIR.mkdir(parents=True, exist_ok=True)
            (CIRCUITS_DIR / f"{session['circuit_key']}.json").write_text(
                json.dumps(circuit, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            log(f"built circuit outline for {session['circuit_short_name']}")
        else:
            log(f"нет пригодного контура для {session['circuit_short_name']} — "
                "карта будет пустой, попробуем позже")
    bounds = tuple(circuit["bounds"]) if circuit else None

    times, frames = [], []
    if bounds:
        loc_rows, complete = fetch_locations(sk, scan_start, end)
        log(f"location rows: {len(loc_rows)} (scan from {scan_start:%H:%M}Z)")
        if complete:
            times, frames = build_frames(loc_rows, driver_list, start, end, bounds,
                                 (circuit.get("outline") if circuit else None))
        else:
            log("positions incomplete — skipping session, will retry on a later run")
            return None, False

    # --- small endpoints: full session is fine ---
    pit = of1_get(f"pit?session_key={sk}") or []
    time.sleep(API_PAUSE)
    weather = downsample(of1_get(f"weather?session_key={sk}") or [], WEATHER_SAMPLE_SECONDS)
    time.sleep(API_PAUSE)
    radio = of1_get(f"team_radio?session_key={sk}") or []
    time.sleep(API_PAUSE)
    rc = of1_get(f"race_control?session_key={sk}") or []
    time.sleep(API_PAUSE)
    laps = of1_get(f"laps?session_key={sk}") or []
    time.sleep(API_PAUSE)
    stints = of1_get(f"stints?session_key={sk}") or []
    time.sleep(API_PAUSE)
    result = of1_get(f"session_result?session_key={sk}") or []
    time.sleep(API_PAUSE)
    overtakes = of1_get(f"overtakes?session_key={sk}") or []
    time.sleep(API_PAUSE)

    pos_events = build_position_events(positions, driver_list, times)
    log(f"position events: {len(pos_events)} | stints: {len(stints)} "
        f"| result: {len(result)} | overtakes: {len(overtakes)}")

    pit_counts = {}
    for p in pit:
        pit_counts[str(p["driver_number"])] = pit_counts.get(str(p["driver_number"]), 0) + 1

    best = {}
    for l in laps:
        if l.get("lap_duration") is None:
            continue
        k = l["driver_number"]
        if k not in best or l["lap_duration"] < best[k]["lap_duration"]:
            best[k] = l

    current_lap = {}
    for l in laps:
        n = l.get("driver_number")
        if n is None:
            continue
        current_lap[n] = max(current_lap.get(n, 0), l.get("lap_number") or 0)

    tyres = {n: stint_at(stints, n, lap) for n, lap in current_lap.items()}

    return {
        "session_key": sk,
        "session_name": session["session_name"],
        "date_start": start.isoformat(),
        "date_end": end.isoformat(),
        "circuit_key": session["circuit_key"],
        "circuit_short_name": session.get("circuit_short_name", ""),
        "country_code": session.get("country_code", ""),
        # The bounds the frames were mapped with. Without them a rebuilt circuit
        # silently leaves the cars off the track: the positions are already
        # quantised against the old box and cannot be corrected afterwards.
        "bounds": [round(b) for b in bounds] if bounds else [],
        "sample_seconds": POS_SAMPLE_SECONDS,
        # Bumped whenever the stored frames change meaning, so sessions written
        # by an older builder are rebuilt instead of lingering. The pre-session
        # frames were dropped in version 2.
        "frames_version": FRAMES_VERSION,
        "has_positions": bool(frames),
        # OpenF1 has no telemetry for a handful of sessions: every car reports
        # one fixed coordinate for the whole session, which puts them all off
        # the racing line and looks like a broken map. Saying so is better than
        # drawing it wrong — four sessions out of sixty were affected.
        "positions_static": positions_are_static(frames),
        "drivers": drivers,
        "frame_times": times,
        "frames": frames,
        "position_events": pos_events,
        "stints": [{
            "n": s["driver_number"], "compound": s.get("compound"),
            "from": s.get("lap_start"), "to": s.get("lap_end"),
            "no": s.get("stint_number"),
        } for s in stints],
        "final_tyres": tyres,
        "session_result": [{
            "n": r.get("driver_number"), "pos": r.get("position"),
            "laps": r.get("number_of_laps"), "dur": r.get("duration"),
            "gap": r.get("gap_to_leader"), "dnf": r.get("dnf"), "dsq": r.get("dsq"),
        } for r in sorted(result, key=lambda x: x.get("position") or 99)],
        "overtakes": [{
            "t": o.get("date"), "over": o.get("overtaking_driver_number"),
            "under": o.get("overtaken_driver_number"), "pos": o.get("position"),
        } for o in sorted(overtakes, key=lambda x: x.get("date") or "")],
        "weather": [{
            "t": w.get("date"), "air": w.get("air_temperature"),
            "track": w.get("track_temperature"), "humidity": w.get("humidity"),
            "wind_speed": w.get("wind_speed"), "wind_dir": w.get("wind_direction"),
            "rainfall": w.get("rainfall"), "pressure": w.get("pressure"),
        } for w in weather],
        "pit": [{
            "n": p["driver_number"], "lap": p.get("lap_number"),
            "dur": p.get("pit_duration"), "lane": p.get("lane_duration"),
            "t": p.get("date"),
        } for p in pit],
        "pit_counts": pit_counts,
        "radio": [{
            "n": r["driver_number"], "t": r.get("date"), "url": r.get("recording_url"),
        } for r in radio if r.get("recording_url")],
        "race_control": [{
            "t": r.get("date"), "msg": r.get("message"), "cat": r.get("category"),
            "flag": r.get("flag"), "lap": r.get("lap_number"),
        } for r in rc],
        "best_laps": [{
            "n": n, "lap": l.get("lap_number"), "time": fmt_seconds(l.get("lap_duration")),
            "s1": fmt_seconds(l.get("duration_sector_1")),
            "s2": fmt_seconds(l.get("duration_sector_2")),
            "s3": fmt_seconds(l.get("duration_sector_3")),
            "i1": l.get("i1_speed"), "i2": l.get("i2_speed"),
        } for n, l in sorted(best.items())],
        "updated": datetime.now(timezone.utc).isoformat(),
    }, True


# ─── Main ───
def positions_are_static(frames) -> bool:
    """True when the cars never move — i.e. the source has no telemetry.

    OpenF1 returns one fixed coordinate per car for sessions it did not record
    properly. Drawn on the outline, that puts the whole field at a single point
    well away from the track, which reads as a broken map rather than as a
    missing one.
    """
    if len(frames) < 5:
        return True
    def signature(frame):
        return tuple(frame[k] for k in range(0, min(len(frame), 20), 2))
    return len({signature(f) for f in frames}) <= 1


def bounds_drift(detail, circuit) -> bool:
    """True when a session's stored frames need rebuilding."""
    if detail.get("frames_version") != FRAMES_VERSION:
        return True          # written by an older builder
    if not circuit:
        return False
    stored = detail.get("bounds")
    if not stored:
        return True          # built before bounds were recorded
    return list(stored) != list(circuit["bounds"])


def ensure_circuits(sessions) -> set:
    """Give every circuit in the archive an outline, not just the recent ones.

    Outlines used to be a by-product of building a session, so only the tracks
    of the last few rounds had a map and older ones came up empty. Each
    circuit is built from the telemetry of its own most recent session.
    """
    by_circuit = {}
    for s in sessions:
        key = s.get("circuit_key")
        if key is not None:
            by_circuit.setdefault(key, []).append(s)
    missing = [k for k in sorted(by_circuit) if not circuit_is_usable(load_circuit(k))]
    log(f"трасс в архиве: {len(by_circuit)} | без пригодного контура: {len(missing)}")
    if not missing:
        return set()

    CIRCUITS_DIR.mkdir(parents=True, exist_ok=True)
    rebuilt = set()
    for key in missing[:MAX_CIRCUIT_BUILDS_PER_RUN]:
        # Several sessions, newest first. Location data is not published
        # uniformly: Bahrain has it for testing days but not for the race, and
        # Jeddah has none at all for 2026 while 2025 is complete. The track is
        # the same, so any session with telemetry will do.
        for ses in sorted(by_circuit[key], key=lambda z: z.get("date_end") or "",
                          reverse=True)[:CIRCUIT_SESSION_TRIES]:
            sk = ses["session_key"]
            start = parse_dt(ses.get("date_start"))
            drivers = of1_get(f"drivers?session_key={sk}") or []
            nums = [d.get("driver_number") for d in drivers
                    if d.get("driver_number") is not None]
            if not nums or start is None:
                continue
            circuit = build_outline_from_any_driver(key, ses, sk, nums, start)
            if circuit:
                (CIRCUITS_DIR / f"{key}.json").write_text(
                    json.dumps(circuit, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
                rebuilt.add(key)
                log(f"контур построен: {circuit['circuit_short_name']} "
                    f"по сессии {sk}")
                break
            time.sleep(API_PAUSE)
        time.sleep(API_PAUSE)
    if missing:
        log(f"осталось без контура: {len(missing) - len(rebuilt)} "
            "(достроится в следующих запусках)")
    return rebuilt


def main():
    print("=" * 60)
    print("F1 Live View builder (OpenF1)")
    print(f"min year: {MIN_YEAR} | position sample: {POS_SAMPLE_SECONDS}s")
    print("=" * 60)

    print("\n[index] fetching session list…")
    sessions = of1_get("sessions")
    if not sessions:
        print("  OpenF1 unavailable — skipping Live View update")
        return

    now = datetime.now(timezone.utc)
    eligible = sorted(
        [s for s in sessions
         if s.get("year", 0) >= MIN_YEAR
         and s.get("date_end") and parse_dt(s["date_end"]) < now],
        key=lambda s: s["date_end"], reverse=True,
    )
    print(f"  {len(sessions)} sessions total | {len(eligible)} eligible (>= {MIN_YEAR}, finished)")

    existing = {int(p.stem) for p in SESSIONS_DIR.glob("*.json")} if SESSIONS_DIR.exists() else set()
    rebuilt_circuits = ensure_circuits(eligible)

    # A session whose circuit was rebuilt has frames mapped against the old
    # bounds, so its cars sit off the new outline. Those are rebuilt as well.
    stale = set()
    if SESSIONS_DIR.exists():
        for p in SESSIONS_DIR.glob("*.json"):
            try:
                detail = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                stale.add(int(p.stem))
                continue
            circuit = load_circuit(detail.get("circuit_key"))
            if (detail.get("circuit_key") in rebuilt_circuits
                    or bounds_drift(detail, circuit)):
                stale.add(int(p.stem))
    if stale:
        log(f"сессий с устаревшими границами: {len(stale)} — будут пересобраны")

    # Only sessions inside the retention window are worth building detail for,
    # so a long gap never backfills hundreds of stale files.
    in_window = eligible[:DETAIL_KEEP_RECENT]
    # Sessions whose positions were mapped against the wrong bounds come first.
    # Ordering by date alone let them starve: new sessions kept taking the slots
    # and the broken ones stayed broken, because they are the older ones.
    urgent = [s for s in in_window
              if s["session_key"] in stale or s["circuit_key"] in rebuilt_circuits]
    fresh = [s for s in in_window if s["session_key"] not in existing
             and s not in urgent]
    todo = (urgent + fresh)[:MAX_NEW_SESSIONS_PER_RUN]
    log(f"in retention window: {len(in_window)} | already built: {len(existing)} "
        f"| building now: {len(todo)}")

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    built = []
    for s in todo:
        detail, complete = build_session(s)
        if not detail or not complete:
            continue
        path = SESSIONS_DIR / f"{s['session_key']}.json"
        path.write_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        size = path.stat().st_size
        built.append(s["session_key"])
        log(f"saved {path.name} ({size/1024:.0f} KB, "
            f"{len(detail['frames'])} frames, {len(detail['drivers'])} drivers)")

    # prune detail files outside the retention window to bound repo growth
    keep = {s["session_key"] for s in eligible[:DETAIL_KEEP_RECENT]}
    removed = 0
    for p in SESSIONS_DIR.glob("*.json"):
        if int(p.stem) not in keep:
            p.unlink()
            removed += 1
    if removed:
        log(f"pruned {removed} detail files outside the last {DETAIL_KEEP_RECENT} sessions")

    have = {int(p.stem) for p in SESSIONS_DIR.glob("*.json")}
    index = {
        "updated": now.isoformat(),
        "min_year": MIN_YEAR,
        "position_sample_seconds": POS_SAMPLE_SECONDS,
        "sessions": [{
            "key": s["session_key"],
            "name": s["session_name"],
            "type": s.get("session_type", ""),
            "start": s["date_start"],
            "end": s["date_end"],
            "year": s["year"],
            "circuit_key": s["circuit_key"],
            "circuit": s.get("circuit_short_name", ""),
            "country": s.get("country_code", ""),
            "detail": s["session_key"] in have,
        } for s in eligible],
        "circuits": sorted({s["circuit_key"] for s in eligible}),
    }
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                          encoding="utf-8")
    print(f"\n  index: {INDEX_PATH.stat().st_size/1024:.0f} KB, "
          f"{len(index['sessions'])} sessions, {len(have)} with detail")
    print("done")


if __name__ == "__main__":
    main()