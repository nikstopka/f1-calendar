"""
Offline verification for livefeed.LiveState.

Replays F1's own static archive for a finished session through the tracker and
compares the result with FastF1's `Session.load()` output — the path we already
trust. The archive uses byte-identical encoding to the live stream, so this
exercises the same parsing code.

Run:  python test_livefeed.py
"""
import base64
import gzip
import io
import json
import sys
import urllib.request
import zlib
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from livefeed import LiveState

ARCHIVE = "https://livetiming.formula1.com/static/2026/2026-09-26_Azerbaijan_Grand_Prix/2026-09-26_Race/"
TOPICS = ["Position.z", "CarData.z", "TimingData", "TimingAppData",
          "WeatherData", "TrackStatus", "LapCount", "RaceControlMessages",
          "DriverList"]

failures = []


def check(label, got, want, tol=None):
    if tol is not None:
        ok = got is not None and want is not None and abs(float(got) - float(want)) <= tol
    else:
        ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'} {label}: live={got} vs load={want}")
    if not ok:
        failures.append(label)


def archive_lines(topic):
    req = urllib.request.Request(ARCHIVE + topic + ".jsonStream",
                                 headers={"User-Agent": "Mozilla/5.0",
                                          "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace").splitlines()


def main():
    print("=" * 62)
    print("Проверка livefeed против FastF1 Session.load()")
    print("=" * 62)

    state = LiveState()
    counts = {}
    print("\n[1] воспроизведение архива")
    for topic in TOPICS:
        try:
            lines = archive_lines(topic)
        except Exception as e:
            print(f"  {topic}: пропущен ({str(e)[:50]})")
            continue
        n = 0
        for line in lines:
            if state.feed(topic, line):
                n += 1
        counts[topic] = n
        print(f"  {topic:22} строк={len(lines):6}  принято={n:6}")

    print("\n[2] эталон из FastF1")
    import os

    import fastf1
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", ".ffcache")
    os.makedirs(cache, exist_ok=True)          # FastF1 требует готовый каталог
    fastf1.Cache.enable_cache(cache)
    ev = fastf1.get_event(2026, "Azerbaijan")
    sess = ev.get_session("R")
    sess.load(telemetry=True)
    print(f"  загружено: pos_data драйверов={len(sess.pos_data)}, "
          f"laps={len(sess.laps)}, водителей={len(sess.drivers)}")

    print("\n[3] сверка")
    num = 63
    # FastF1 keeps DriverNumber as a string column — normalise once
    lap_num = sess.laps.assign(_n=sess.laps["DriverNumber"].astype(int))
    lap_num = lap_num[lap_num["_n"] == num]

    # --- позиция на траске: последняя строка pos_data ---
    ref = sess.pos_data[str(num)].dropna(subset=["X", "Y"]).iloc[-1]
    got = state.cars.get(num, {})
    check(f"driver {num} X", got.get("x"), float(ref["X"]), tol=60)
    check(f"driver {num} Y", got.get("y"), float(ref["Y"]), tol=60)

    # --- финальная позиция в зачёте ---
    check(f"driver {num} position", state.positions.get(num),
          int(lap_num.iloc[-1]["Position"]))

    # --- количество машин с координатами ---
    ref_cars = sum(1 for k, df in sess.pos_data.items()
                   if df is not None and not df.empty and "X" in df.columns
                   and not df.dropna(subset=["X", "Y"]).empty)
    snap = state.snapshot()
    check("машин в снимке (без машин безопасности)", len(snap["cars"]), ref_cars)
    check("пилотов в снимке", len(snap["drivers"]), len(sess.drivers))

    # --- погода ---
    w = sess.weather_data.iloc[-1]
    check("погода: воздух", state.weather.get("air"), float(w["AirTemp"]), tol=0.6)
    check("погода: асфальт", state.weather.get("track"), float(w["TrackTemp"]), tol=0.6)
    check("погода: влажность", state.weather.get("humidity"), float(w["Humidity"]), tol=0.6)

    # --- статус трассы ---
    check("статус трассы", state.track_status,
          str(sess.track_status.iloc[-1]["Status"]))

    # --- лучший круг ---
    laps_num = lap_num[lap_num["LapTime"].notna()]
    import pandas as pd
    ref_best = pd.Timedelta(laps_num.loc[laps_num["LapTime"].idxmin()]["LapTime"]).total_seconds()
    got_best = state.best_lap.get(num, {}).get("time")
    check(f"driver {num} лучший круг", got_best, ref_best, tol=0.05)

    # --- пилоты ---
    print(f"  пилотов в трекере: {len(state.drivers)} "
          f"(в load(): {len(sess.drivers)})")
    if state.drivers:
        d = state.drivers.get(num) or list(state.drivers.values())[0]
        print(f"    пример: #{d['n']} {d['name']} / {d['team']} {d['team_color']}")

    # --- пит-стопы ---
    # Эталон: у каждого пилота в load() есть столбец Stint с номером текущего
    # стинта. Сверяем всех, кто отработал хотя бы один стинт, — именно этот
    # случай раньше молча ломался на списочной форме поля Stints.
    ref_stints = {}
    for dnum in sess.drivers:                # FastF1 отдаёт список номеров
        rows = sess.laps[sess.laps["DriverNumber"].astype(int) == int(dnum)]
        if "Stint" in rows.columns and rows["Stint"].notna().any():
            ref_stints[int(dnum)] = int(rows["Stint"].max())
    got_stints = state.stint
    common = {k: v for k, v in got_stints.items() if k in ref_stints}
    print(f"  пит-стопов: в трекере {len(got_stints)}, "
          f"в load() {len(ref_stints)}, совпали по {len(common)} пилотам")
    bad = {k: (got_stints[k], ref_stints[k])
           for k in common if got_stints[k] != ref_stints[k]}
    for k, (a, b) in list(bad.items())[:3]:
        print(f"    расхождение #{k}: трекер={a} load={b}")
    check("расхождений в номерах стинтов", len(bad), 0)
    check("пилотов со стинтами не потеряно", len(got_stints), len(ref_stints))

# --- резина на финише ---
    # Эталон — соединение последнего круга пилота; TimingAppData отдаёт
    # соединение того же стинта, поэтому значения должны совпасть.
    with_compound = lap_num[lap_num["Compound"].astype(str).str.len() > 0]
    ref_compound = str(with_compound.iloc[-1]["Compound"]).upper()
    check(f"driver {num} резина", (state.tyre.get(num) or "").upper(),
          ref_compound)

    # --- радио-переговоры ---
    rc_ref = sess.race_control_messages
    print(f"  дирекция: в трекере {len(state.race_control)}, "
          f"в load() {len(rc_ref)}")
    if state.race_control and len(rc_ref):
        print(f"    пример: {state.race_control[0]['msg']}")

    # --- память ---
    import tracemalloc
    tracemalloc.start()
    st2 = LiveState()
    for topic in ("Position.z", "CarData.z", "TimingData"):
        for line in archive_lines(topic):
            st2.feed(topic, line)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"\n[4] память трекера: {cur/1048576.0:.2f} МБ (пик {peak/1048576.0:.2f} МБ) "
          f"на {len(st2.cars)} машин")
    print(f"    для сравнения: Session.load() = 777 МБ приватной памяти")

    print("\n" + "=" * 62)
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — " + ", ".join(failures))
        return 1
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 0


if __name__ == "__main__":
    sys.exit(main())