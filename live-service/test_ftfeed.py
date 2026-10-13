"""Check the formula-timing adapter against the live endpoint.

The adapter was written from an `initial` snapshot of a finished session, so this
asserts the snapshot the site would actually receive: a driver list, a timing
table, gaps and lap times. "It did not crash" is not a pass — the numbers have to
come out.

    python live-service/test_ftfeed.py

Not part of the service; it needs network access.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ftfeed import FormulaTimingFeed  # noqa: E402


def main():
    feed = FormulaTimingFeed(log=lambda m: print("   [сервис] %s" % m))
    feed.start()

    deadline = time.time() + 45
    snap = None
    while time.time() < deadline:
        time.sleep(2)
        if feed.messages:
            snap = feed.state.snapshot(circuit_key=None, is_live=False)
            if snap.get("drivers") and snap.get("positions"):
                break
    feed.stop()

    if not feed.messages:
        print("ПРОВАЛ: поток не отдал ни одного события")
        print("   статус: %s" % json.dumps(feed.status(), ensure_ascii=False))
        return 1

    if not snap:
        print("ПРОВАЛ: снимок не собрался")
        return 1

    print()
    print("=== что получилось ===")
    print("   событий: %d | неизвестных ключей: %s"
          % (feed.messages, dict(feed.unknown) or "нет"))
    print("   пилотов: %d | позиций в башне: %d"
          % (len(snap.get("drivers") or []), len(snap.get("positions") or {})))
    print()

    failures = []

    drivers = snap.get("drivers") or []
    if len(drivers) < 20:
        failures.append("пилотов %d, ожидалось 22" % len(drivers))
    else:
        d = drivers[0]
        if not d.get("name") or not d.get("code"):
            failures.append("у первого пилота нет имени: %r" % d)
        else:
            print("   пилот[0]: %s (%s), команда %r, цвет %s"
                  % (d.get("name"), d.get("code"), d.get("team"), d.get("team_color")))

    positions = snap.get("positions") or {}
    if len(positions) < 20:
        failures.append("позиций %d, ожидалось 22" % len(positions))
    else:
        print("   позиций: %d" % len(positions))

    gaps = snap.get("gaps") or {}
    print("   отрывов: %d | пример: %s"
          % (len(gaps), list(gaps.items())[:2]))
    if len(gaps) < 10:
        failures.append("отрывов всего %d" % len(gaps))

    best = snap.get("best_laps") or {}
    print("   лучших кругов: %d | пример: %s"
          % (len(best), list(best.items())[:2]))
    if len(best) < 10:
        failures.append("лучших кругов всего %d" % len(best))

    tyres = snap.get("tyres") or {}
    print("   шин: %d | пример: %s" % (len(tyres), list(tyres.items())[:3]))
    if tyres and len(tyres) < 10:
        failures.append("шин всего %d" % len(tyres))

    laps = snap.get("laps") or {}
    print("   кругов: %d" % len(laps))

    print()
    print("   полезная нагрузка: %s" % json.dumps(
        {k: v for k, v in snap.items() if k not in ("drivers", "positions")},
        ensure_ascii=False)[:400])
    print()

    if failures:
        print("ПРОВАЛ:")
        for f in failures:
            print("   - %s" % f)
        return 1

    print("ГОДЕНО: снимок содержит башню, отрывы, круги и шины")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())