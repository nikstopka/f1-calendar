"""
Smoke-test of the poll_loop state machine in live-only mode.

No network and no FastF1: the feed is replaced by a stub so the three
transitions can be driven deterministically:

    waiting stub  ->  live session  ->  session ended  ->  waiting stub

Run:  python -m test_poll_loop
"""
import asyncio
import base64
import json
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app  # noqa: E402
from livefeed import LiveState  # noqa: E402


def zmsg(payload: dict) -> str:
    """Same wire format as Position.z: base64 of headerless raw zlib."""
    raw = json.dumps(payload).encode()
    return base64.b64encode(zlib.compress(raw)[2:-4]).decode()


class StubFeed:
    """Mimics livews.LiveFeed: just the bits app.py actually touches."""

    def __init__(self):
        self.state = LiveState()
        self._connected = False
        self._messages = 0
        self._idle = None

    def status(self):
        return {"connected": self._connected, "messages": self._messages,
                "idle_seconds": self._idle, "cars": len(self.state.cars),
                "drivers": len(self.state.drivers)}


def go_live(f: StubFeed):
    f._connected = True
    f.state.feed("DriverList", json.dumps({
        "44": {"RacingName": "Lewis HAMILTON", "FullName": "Lewis Hamilton",
               "TeamName": "Ferrari", "Line": 1, "Number": "44"},
        "63": {"RacingName": "George RUSSELL", "FullName": "George Russell",
               "TeamName": "Mercedes", "Line": 2, "Number": "63"},
    }))
    f.state.feed("SessionInfo", json.dumps({"Entries": [
        {"Utc": "2026-10-11T13:00:00", "Name": "Race", "Session": "Race",
         "Path": "2026/2026-10-11_Singapore_Grand_Prix/2026-10-11_Race/",
         "Gaming": {"Key": 99999, "Meeting": 1200, "Session": 99999}}]}))
    f.state.feed("WeatherData", json.dumps({
        "t": "2026-10-11T13:00:00", "AirTemp": "24.1", "TrackTemp": "31.7",
        "Humidity": "0.62", "WindSpeed": "11", "WindDirection": "180",
        "Pressure": "1012"}))
    f.state.feed("Position.z", zmsg({"Entries": [
        {"Timestamp": 1759893000000, "Entries": {
            "44": {"X": 1234.5, "Y": -678.9, "Z": 0.4, "Position": 2},
            "63": {"X": 876.0, "Y": -544.0, "Z": 0.3, "Position": 1}}}]}))
    f.state.feed("CarData.z", zmsg({"Entries": [
        {"Utc": "2026-10-11T13:10:00.100", "Cars": {
            "44": {"Channels": {"Speed": 310, "Throttle": 0.9,
                                "Brake": 0, "Gear": 8}},
            "63": {"Channels": {"Speed": 305, "Throttle": 1.0,
                                "Brake": 0, "Gear": 8}}}}]}))
    f.state.feed("TimingData", json.dumps({"Lines": {
        "44": {"Position": "2", "LapNumber": "18", "GapToLeader": "+0.412",
               "LastLapTime": "1:32.101", "BestLapTime": "1:31.004"},
        "63": {"Position": "1", "LapNumber": "18", "GapToLeader": "Leader",
               "LastLapTime": "1:31.998", "BestLapTime": "1:30.777"}}}))
    # Реальный поток: Stints — словарь, ключ которого и есть номер стинта.
    f.state.feed("TimingAppData", json.dumps({"Lines": {
        "44": {"Stints": {"1": {"Compound": "SOFT", "TotalLaps": 4,
                                "StartLaps": 14}}},
        "63": {"Stints": {"0": {"Compound": "MEDIUM", "TotalLaps": 18,
                                "StartLaps": 0}}}}}))
    f._messages = 1200
    f._idle = 0.4


def go_quiet(f: StubFeed):
    f._connected = True
    f._messages = 1200
    f._idle = 400          # no traffic for a long while -> session is over
    f.state.cars.clear()
    f.state.drivers.clear()
    f.state.positions.clear()


def main() -> int:
    app.ARCHIVE_FALLBACK = False
    app.POLL_SECONDS = 0.05
    app.GRID = 1000
    stub = StubFeed()
    app.feed = stub

    async def drive():
        task = asyncio.create_task(app.poll_loop())
        checks = []

        def check(name, cond, detail=""):
            checks.append((name, cond, detail))
            print(("  OK   " if cond else "  FAIL ") + name
                  + (f"  [{detail}]" if detail else ""))

        await asyncio.sleep(0.4)
        s = app._state["snapshot"]
        check("старт: заглушка ожидания", s.get("waiting_for_session") is True)
        check("заглушка: is_live = false", s.get("is_live") is False)
        check("заглушка: пустые списки, не null",
              s.get("cars") == [] and s.get("positions") == {})
        check("заглушка: ключи совпадают с боевым снимком",
              all(k in s for k in ("drivers", "cars", "positions", "gaps",
                                   "best_laps", "tyres", "stints", "weather",
                                   "race_control")))
        check("ожидание: source = livefeed", app._state["source"] == "livefeed")

        go_live(stub)
        await asyncio.sleep(0.5)
        s = app._state["snapshot"]
        check("live: заглушка заменена боевым снимком",
              not s.get("waiting_for_session"))
        check("live: 2 машины", len(s.get("cars") or []) == 2,
              str(len(s.get("cars") or [])))
        check("live: позиции 1 и 2",
              sorted(s.get("positions", {}).values()) == [1, 2],
              json.dumps(s.get("positions")))
        check("live: X/Y координаты сохранены",
              any(abs(c["x"] - 876.0) < 0.01 for c in s.get("cars") or []))
        check("live: погода разобрана",
              abs((s.get("weather") or {}).get("air", 0) - 24.1) < 0.01,
              json.dumps(s.get("weather"), ensure_ascii=False)[:70])
        check("live: отрыв до лидера",
              (s.get("gaps") or {}).get(63) == "Leader",
              json.dumps(s.get("gaps"), ensure_ascii=False))
        check("live: номер пит-стопа (2-й стинт)", (s.get("stints") or {}).get(44) == 2,
              json.dumps(s.get("stints")))
        check("live: номер пит-стопа (1-й стинт)", (s.get("stints") or {}).get(63) == 1,
              json.dumps(s.get("stints")))
        check("live: резина из TimingAppData",
              (s.get("tyres") or {}).get(44) == "SOFT",
              json.dumps(s.get("tyres")))
        check("live: лучший круг", (s.get("best_laps") or {}).get(63, {}).get("time") == 90.777,
              json.dumps(s.get("best_laps")))
        check("live: лучший круг не потерян", "63" in json.dumps(s.get("drivers")),
              json.dumps(s.get("drivers"), ensure_ascii=False)[:90])
        check("live: is_live = true", app._state["is_live"] is True)

        go_quiet(stub)
        await asyncio.sleep(0.5)
        s = app._state["snapshot"]
        check("после финиша: возврат к заглушке",
              s.get("waiting_for_session") is True)
        check("после финиша: координаты очищены", s.get("cars") == [])
        check("после финиша: session_key сброшен",
              app._state["session_key"] is None)
        check("после финиша: is_live = false", app._state["is_live"] is False)

        go_live(stub)
        await asyncio.sleep(0.5)
        check("вторая сессия подхватывается снова",
              not app._state["snapshot"].get("waiting_for_session")
              and app._state["is_live"])

        task.cancel()
        return checks

    checks = asyncio.run(drive())
    bad = [n for n, ok, _ in checks if not ok]
    print("\n" + "=" * 62)
    print(f"ПРОВАЛЕНО: {len(bad)}" + (" — " + ", ".join(bad) if bad else "")
          if bad else "ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())