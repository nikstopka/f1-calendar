#!/usr/bin/env python3
"""
F1 Calendar — Multi-Season Automated Data Update

Fetches schedule, results, qualifying, sprint, and standings data from the
Jolpica F1 API for the current year + previous 10 years.

Generates:
  - data/seasons/{year}.json  (per-season data)
  - data/index.json           (season index)
  - f1-calendar.ics           (ICS for current year only)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ─── Config ───
API_BASE = "https://api.jolpi.ca/ergast/f1"
CURRENT_YEAR = datetime.now(timezone.utc).year
HISTORY_YEARS = 10  # previous years to include
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"
SEASONS_DIR = DATA_DIR / "seasons"
ICS_PATH = PROJECT_DIR / "f1-calendar.ics"
INDEX_PATH = DATA_DIR / "index.json"

SESSION_RU = {
    "FirstPractice": "Свободная практика 1",
    "SecondPractice": "Свободная практика 2",
    "ThirdPractice": "Свободная практика 3",
    "SprintQualifying": "Спринт-квалификация",
    "Sprint": "Спринт",
    "Qualifying": "Квалификация",
    "Race": "Гонка",
}

SESSION_ORDER = [
    "FirstPractice", "SprintQualifying", "Sprint",
    "SecondPractice", "Qualifying", "ThirdPractice", "Race",
]

SESSION_DUR = {
    "FirstPractice": 60, "SecondPractice": 60, "ThirdPractice": 60,
    "SprintQualifying": 45, "Sprint": 60,
    "Qualifying": 60, "Race": 120,
}


# ─── API helpers ───
def api_get(path, retries=3):
    url = f"{API_BASE}/{path}"
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": "F1-Calendar-Bot/1.0"})
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                print(f"  Rate limited, waiting 10s (attempt {attempt+1})")
                time.sleep(10)
            else:
                print(f"  API error {resp.status_code} for {path} (attempt {attempt+1})")
                time.sleep(2)
        except Exception as e:
            print(f"  Request error for {path}: {e} (attempt {attempt+1})")
            time.sleep(3)
    print(f"  Failed to fetch {path} after {retries} attempts")
    return None


def parse_session_dt(date_str, time_str):
    """Parse API date+time into UTC datetime. Returns None if no time."""
    if not time_str or time_str == "N/A":
        if not date_str:
            return None
        # Date only — assume 14:00 UTC as placeholder
        return datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    dt_str = f"{date_str}T{time_str.replace('Z', '')}"
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


def fetch_schedule(year):
    """Fetch race schedule for a season from the API."""
    data = api_get(f"{year}.json")
    if not data:
        return []
    races = data["MRData"]["RaceTable"].get("Races", [])
    result = []
    for race in races:
        round_num = int(race["round"])
        circuit = race.get("Circuit", {})
        loc = circuit.get("Location", {})
        sprint = "Sprint" in race
        sessions = []

        for session_key in SESSION_ORDER:
            if session_key in race:
                sess = race[session_key]
                dt = parse_session_dt(sess.get("date", ""), sess.get("time", ""))
                if dt:
                    sessions.append((session_key, dt))

        # Race session is at the top level (date/time), not under a 'Race' key
        race_date = race.get("date", "")
        race_time = race.get("time", "")
        race_dt = parse_session_dt(race_date, race_time)
        if race_dt:
            sessions.append(("Race", race_dt))

        # Sort sessions chronologically
        sessions.sort(key=lambda x: x[1])

        result.append({
            "round": round_num,
            "gp": race.get("raceName", f"Round {round_num}"),
            "circuit": circuit.get("circuitName", ""),
            "location": f"{loc.get('locality', '')}, {loc.get('country', '')}",
            "length_km": None,  # Not available from API schedule
            "sprint": sprint,
            "sessions": sessions,
        })
    return result


def fetch_all_results(year):
    """Fetch all race results for a season, paginating through all rounds."""
    by_round = {}
    offset = 0
    while True:
        data = api_get(f"{year}/results.json?limit=100&offset={offset}")
        if not data:
            break
        races = data["MRData"]["RaceTable"].get("Races", [])
        if not races:
            break
        for race in races:
            round_num = int(race["round"])
            if round_num not in by_round:
                by_round[round_num] = []
            for r in race.get("Results", []):
                driver = r["Driver"]
                constructor = r.get("Constructor", {})
                status = r.get("status", "")
                time_str = r.get("Time", {}).get("time", "—") if r.get("Time") else "—"
                if status not in ("Finished", "+1 Lap"):
                    time_str = status
                by_round[round_num].append({
                    "pos": r.get("positionText", str(r.get("position", ""))),
                    "driver_id": driver.get("driverId", ""),
                    "driver": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                    "code": driver.get("code", ""),
                    "team_id": constructor.get("constructorId", ""),
                    "team": constructor.get("name", ""),
                    "grid": int(r.get("grid", 0)),
                    "time": time_str,
                    "points": int(float(r.get("points", 0))),
                })
        total_returned = sum(len(r.get("Results", [])) for r in races)
        offset += total_returned
        if offset >= int(data["MRData"]["total"]) or total_returned == 0:
            break
        time.sleep(1)
    return by_round


def fetch_all_sprints(year):
    """Fetch all sprint results for a season, paginating."""
    by_round = {}
    offset = 0
    while True:
        data = api_get(f"{year}/sprint.json?limit=100&offset={offset}")
        if not data:
            break
        races = data["MRData"]["RaceTable"].get("Races", [])
        if not races:
            break
        for race in races:
            round_num = int(race["round"])
            if round_num not in by_round:
                by_round[round_num] = []
            for r in race.get("SprintResults", []):
                driver = r["Driver"]
                constructor = r.get("Constructor", {})
                status = r.get("status", "")
                time_str = r.get("Time", {}).get("time", "—") if r.get("Time") else "—"
                if status not in ("Finished", "+1 Lap"):
                    time_str = status
                by_round[round_num].append({
                    "pos": r.get("positionText", str(r.get("position", ""))),
                    "driver_id": driver.get("driverId", ""),
                    "driver": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                    "code": driver.get("code", ""),
                    "team_id": constructor.get("constructorId", ""),
                    "team": constructor.get("name", ""),
                    "grid": int(r.get("grid", 0)),
                    "time": time_str,
                    "points": int(float(r.get("points", 0))),
                })
        total_returned = sum(len(r.get("SprintResults", [])) for r in races)
        offset += total_returned
        if offset >= int(data["MRData"]["total"]) or total_returned == 0:
            break
        time.sleep(1)
    return by_round


def fetch_all_qualifying(year):
    """Fetch all qualifying results for a season, paginating."""
    by_round = {}
    offset = 0
    while True:
        data = api_get(f"{year}/qualifying.json?limit=100&offset={offset}")
        if not data:
            break
        races = data["MRData"]["RaceTable"].get("Races", [])
        if not races:
            break
        for race in races:
            round_num = int(race["round"])
            if round_num not in by_round:
                by_round[round_num] = []
            for q in race.get("QualifyingResults", []):
                driver = q["Driver"]
                constructor = q.get("Constructor", {})
                by_round[round_num].append({
                    "pos": int(q["position"]),
                    "driver_id": driver.get("driverId", ""),
                    "driver": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                    "code": driver.get("code", ""),
                    "team": constructor.get("name", ""),
                    "q1": q.get("Q1", "—"),
                    "q2": q.get("Q2", "—"),
                    "q3": q.get("Q3", "—"),
                })
        total_returned = sum(len(r.get("QualifyingResults", [])) for r in races)
        offset += total_returned
        if offset >= int(data["MRData"]["total"]) or total_returned == 0:
            break
        time.sleep(1)
    return by_round


def fetch_standings(year):
    """Fetch final/current driver and constructor standings."""
    ds_data = api_get(f"{year}/driverstandings.json")
    drivers = []
    if ds_data:
        table = ds_data["MRData"]["StandingsTable"]
        sl = table.get("StandingsLists", [{}])[0]
        round_num = int(sl.get("round", 0))
        for s in sl.get("DriverStandings", []):
            drivers.append({
                "pos": int(s.get("position", 0)) if s.get("position") else 999,
                "driver_id": s["Driver"].get("driverId", ""),
                "driver": f"{s['Driver'].get('givenName', '')} {s['Driver'].get('familyName', '')}".strip(),
                "code": s["Driver"].get("code", ""),
                "team": s["Constructors"][0]["name"] if s.get("Constructors") else "",
                "points": int(float(s.get("points", 0))),
                "wins": int(s.get("wins", 0)),
            })
    else:
        round_num = 0

    cs_data = api_get(f"{year}/constructorstandings.json")
    constructors = []
    if cs_data:
        sl = cs_data["MRData"]["StandingsTable"].get("StandingsLists", [{}])[0]
        for s in sl.get("ConstructorStandings", []):
            constructors.append({
                "pos": int(s.get("position", 0)) if s.get("position") else 999,
                "team": s["Constructor"]["name"],
                "points": int(float(s.get("points", 0))),
                "wins": int(s.get("wins", 0)),
            })

    return drivers, constructors, round_num


def compute_points_tables(schedule, all_results, all_sprints):
    """For each round, compute a unified table: driver, team, sprint pts, race pts, total after.
    Accumulates points across the season.
    """
    cumulative = {}  # driver_id -> {driver, team, points}
    tables_by_round = {}

    for race in schedule:
        round_num = race["round"]
        race_results = all_results.get(round_num, [])
        sprint_results = all_sprints.get(round_num, [])

        # Build maps for this round
        race_map = {}
        for r in race_results:
            race_map[r["driver_id"]] = r
        sprint_map = {}
        for r in sprint_results:
            sprint_map[r["driver_id"]] = r

        # Collect all driver IDs that appeared in this round
        all_ids = set(race_map.keys()) | set(sprint_map.keys())

        rows = []
        for did in all_ids:
            r = race_map.get(did, {})
            sp = sprint_map.get(did, {})
            sprint_pts = sp.get("points", None)  # None = no sprint this weekend
            race_pts = r.get("points", 0)
            driver_name = r.get("driver") or sp.get("driver", "")
            team_name = r.get("team") or sp.get("team", "")

            # Accumulate
            if did not in cumulative:
                cumulative[did] = {"driver": driver_name, "team": team_name, "points": 0}
            cumulative[did]["points"] += race_pts
            if sprint_pts is not None:
                cumulative[did]["points"] += sprint_pts

            total_after = cumulative[did]["points"]

            rows.append({
                "driver_id": did,
                "driver": driver_name,
                "team": team_name,
                "sprint_pts": sprint_pts,
                "race_pts": race_pts,
                "total_after": total_after,
            })

        # Sort by total after race (descending)
        rows.sort(key=lambda x: x["total_after"], reverse=True)
        for i, row in enumerate(rows):
            row["pos"] = i + 1

        tables_by_round[round_num] = rows

    return tables_by_round


def compute_deltas(tables_by_round, schedule):
    """Add delta (points change) to each row based on previous round."""
    prev_totals = {}  # driver_id -> points

    for race in schedule:
        round_num = race["round"]
        table = tables_by_round.get(round_num, [])
        for row in table:
            did = row["driver_id"]
            row["delta"] = row["total_after"] - prev_totals.get(did, 0)
            prev_totals[did] = row["total_after"]

    return tables_by_round


def utc_to_msk(dt):
    return dt + timedelta(hours=3)


# ─── JSON generation ───
def generate_season_json(year, schedule, all_results, all_sprints, all_quali,
                         driver_standings, constructor_standings, current_round):
    now = datetime.now(timezone.utc)
    has_sprint = any(r["sprint"] for r in schedule)

    # Compute unified points tables
    points_tables = compute_points_tables(schedule, all_results, all_sprints)
    points_tables = compute_deltas(points_tables, schedule)

    races = []
    for race in schedule:
        round_num = race["round"]
        race_dt = race["sessions"][-1][1] if race["sessions"] else now
        first_dt = race["sessions"][0][1] if race["sessions"] else now
        if now > race_dt + timedelta(hours=3):
            status = "completed"
        elif now >= first_dt:
            status = "ongoing"
        else:
            status = "upcoming"

        race_data = {
            "round": round_num,
            "gp": race["gp"],
            "circuit": race["circuit"],
            "location": race["location"],
            "sprint": race["sprint"],
            "status": status,
            "sessions": [],
        }

        for session_key, dt in race["sessions"]:
            dt_end = dt + timedelta(minutes=SESSION_DUR.get(session_key, 60))
            msk = utc_to_msk(dt)
            race_data["sessions"].append({
                "name": session_key,
                "name_ru": SESSION_RU.get(session_key, session_key),
                "utc_start": dt.isoformat(),
                "utc_end": dt_end.isoformat(),
                "msk_date": msk.strftime("%d.%m.%Y"),
                "msk_time": msk.strftime("%H:%M"),
            })

        if round_num in all_quali and all_quali[round_num]:
            race_data["qualifying_results"] = all_quali[round_num]
        if round_num in all_sprints and all_sprints[round_num]:
            race_data["sprint_results"] = all_sprints[round_num]
        if round_num in all_results and all_results[round_num]:
            race_data["race_results"] = all_results[round_num]
        if round_num in points_tables and points_tables[round_num]:
            race_data["points_table"] = points_tables[round_num]

        races.append(race_data)

    return {
        "season": year,
        "updated": now.isoformat(),
        "current_round": current_round,
        "has_sprint": has_sprint,
        "driver_standings": driver_standings,
        "constructor_standings": constructor_standings,
        "races": races,
    }


# ─── ICS generation (current year only) ───
def escape_ics(text):
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def format_ics_dt(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def generate_ics(season_data):
    now = datetime.now(timezone.utc)
    dtstamp = format_ics_dt(now)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//F1 Calendar//Perplexity Computer//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:Формула 1 {season_data['season']}",
        "X-WR-TIMEZONE:UTC",
        "X-WR-DESC:Календарь Формулы 1 — все сессии всех этапов",
    ]

    ds = season_data["driver_standings"]
    cs = season_data["constructor_standings"]
    round_num = season_data["current_round"]

    for race in season_data["races"]:
        rn = race["round"]
        quali = race.get("qualifying_results")
        sprint = race.get("sprint_results")
        results = race.get("race_results")
        points_table = race.get("points_table")

        for session in race["sessions"]:
            dt_start = datetime.fromisoformat(session["utc_start"])
            dt_end = datetime.fromisoformat(session["utc_end"])
            uid = f"f1-{season_data['season']}-r{rn:02d}-{session['name'].lower()}@f1-calendar"

            desc_parts = [
                f"Этап {rn} — {race['gp']}",
                f"Трасса: {race['circuit']}",
                f"Место: {race['location']}",
                f"Сессия: {session['name_ru']}",
            ]
            if race["sprint"]:
                desc_parts.append("Формат: Спринт-уикенд")
            desc_parts.append("")

            # Standings after this race from points_table
            if points_table:
                desc_parts.append(f"Зачёт после этапа {rn}:")
                for row in points_table[:10]:
                    desc_parts.append(f"  {row['pos']}. {row['driver']} ({row['team']}) — {row['total_after']} очков")
                desc_parts.append("")

            if session["name"] == "Race" and results:
                desc_parts.append("Результаты гонки:")
                for r in results[:10]:
                    pts = f" (+{r['points']} очков)" if r.get("points", 0) > 0 else ""
                    desc_parts.append(f"  {r['pos']}. {r['driver']} ({r['team']}) — {r['time']}{pts}")
                desc_parts.append("")

            if session["name"] == "Sprint" and sprint:
                desc_parts.append("Результаты спринта:")
                for r in sprint[:10]:
                    pts = f" (+{r['points']} очков)" if r.get("points", 0) > 0 else ""
                    desc_parts.append(f"  {r['pos']}. {r['driver']} ({r['team']}) — {r['time']}{pts}")
                desc_parts.append("")

            if session["name"] in ("Race", "Qualifying") and quali:
                desc_parts.append("Квалификация:")
                for q in quali[:10]:
                    desc_parts.append(f"  {q['pos']}. {q['driver']} ({q['team']}) — {q.get('q3', q.get('q2', q.get('q1', '—')))}")

            description = "\n".join(desc_parts)
            summary = f"F1: {race['gp']} — {session['name_ru']}"
            alarm = f"Через 30 минут: {race['gp']} — {session['name_ru']}"

            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{dtstamp}",
                f"DTSTART:{format_ics_dt(dt_start)}",
                f"DTEND:{format_ics_dt(dt_end)}",
                f"SUMMARY:{escape_ics(summary)}",
                f"DESCRIPTION:{escape_ics(description)}",
                f"LOCATION:{escape_ics(race['location'])}",
                "STATUS:CONFIRMED",
                "BEGIN:VALARM",
                "TRIGGER:-PT30M",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{escape_ics(alarm)}",
                "END:VALARM",
                "END:VEVENT",
            ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ─── Main ───
def update_season(year, is_current=True):
    """Fetch and generate data for a single season."""
    print(f"\n{'='*50}")
    print(f"Season {year}" + (" (CURRENT)" if is_current else " (HISTORY)"))
    print(f"{'='*50}")

    # 1. Schedule
    print("  [1/5] Fetching schedule...")
    schedule = fetch_schedule(year)
    print(f"  {len(schedule)} races found")
    if not schedule:
        print(f"  Skipping {year} — no schedule data")
        return None
    time.sleep(1)

    # 2. Results
    print("  [2/5] Fetching race results...")
    all_results = fetch_all_results(year)
    print(f"  {len(all_results)} rounds with results")
    time.sleep(1)

    # 3. Sprint
    print("  [3/5] Fetching sprint results...")
    all_sprints = fetch_all_sprints(year)
    print(f"  {len(all_sprints)} rounds with sprint results")
    time.sleep(1)

    # 4. Qualifying
    print("  [4/5] Fetching qualifying...")
    all_quali = fetch_all_qualifying(year)
    print(f"  {len(all_quali)} rounds with qualifying")
    time.sleep(1)

    # 5. Standings
    print("  [5/5] Fetching standings...")
    driver_standings, constructor_standings, current_round = fetch_standings(year)
    print(f"  {len(driver_standings)} drivers, {len(constructor_standings)} constructors, round: {current_round}")

    # Generate JSON
    season_data = generate_season_json(
        year, schedule, all_results, all_sprints, all_quali,
        driver_standings, constructor_standings, current_round)

    SEASONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SEASONS_DIR / f"{year}.json"
    out_path.write_text(json.dumps(season_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved: {out_path} ({len(season_data['races'])} races)")

    return season_data


def main():
    print("=" * 60)
    print(f"F1 Multi-Season Data Update")
    print(f"Current year: {CURRENT_YEAR}")
    print(f"Years: {CURRENT_YEAR - HISTORY_YEARS} — {CURRENT_YEAR}")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    years = list(range(CURRENT_YEAR - HISTORY_YEARS, CURRENT_YEAR + 1))

    # Check for --refresh-history flag
    refresh_history = "--refresh-history" in sys.argv

    all_seasons = []

    for year in years:
        is_current = (year == CURRENT_YEAR)
        json_path = SEASONS_DIR / f"{year}.json"

        # Skip historical years if data exists and not refreshing
        if not is_current and json_path.exists() and not refresh_history:
            print(f"\n[{year}] Skipping (cached, use --refresh-history to update)")
            all_seasons.append({"year": year, "cached": True})
            continue

        season_data = update_season(year, is_current)
        if season_data:
            all_seasons.append({"year": year, "cached": False, "races": len(season_data["races"])})

    # Generate ICS for current year
    print(f"\nGenerating ICS for {CURRENT_YEAR}...")
    current_json = SEASONS_DIR / f"{CURRENT_YEAR}.json"
    if current_json.exists():
        season_data = json.loads(current_json.read_text(encoding="utf-8"))
        ics_content = generate_ics(season_data)
        ICS_PATH.write_text(ics_content, encoding="utf-8")
        print(f"  ICS: {ICS_PATH} ({len(ics_content)} bytes)")

    # Generate index
    index = {
        "current_year": CURRENT_YEAR,
        "years": [y for y in years if (SEASONS_DIR / f"{y}.json").exists()],
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Index: {INDEX_PATH} ({len(index['years'])} seasons)")

    print("\nDone!")


if __name__ == "__main__":
    main()
