"""Repair position-event indices, and trim parked frames, on stored sessions.

Why this exists
---------------
Trimming the leading frames of a session shortens `frame_times`, which is the grid
that `position_events` are indexed against. Two things went wrong:

* An in-place trim that replaced `frames` and `frame_times` but left
  `position_events` pointing at the old, longer grid. 35 sessions were affected
  and 21 of them had events past the last frame, so late-race overtakes were
  silently dropped by the browser.
* The trim itself accepted a field parked in the pits, because the pit exit is
  part of the racing surface and the stacked grid sits within any sane distance
  of the outline.

The build path in `update_live_data.py` does not have this problem: it builds
frames, trims them, and only then derives `position_events` from the trimmed
timestamps. This script fixes sessions that were already written to disk.

Why the repair refetches
------------------------
Shifting the stored indices by the number of dropped frames would be free, but
for a session that was already trimmed that number is no longer recorded
anywhere. Re-reading `position` is one cheap request per session and carries its
own timestamps, so every event can be placed on the grid it actually belongs to
without guessing how far the grid moved.

Usage:
    python scripts/repair_live_data.py            # dry run: report only
    python scripts/repair_live_data.py --write    # apply
"""
import argparse
import datetime
import glob
import importlib.util
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("ul", os.path.join(HERE, "update_live_data.py"))
ul = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ul)

SESSIONS_DIR = os.path.join(HERE, "..", "data", "live", "sessions")
CIRCUITS_DIR = os.path.join(HERE, "..", "data", "live", "circuits")

parser = argparse.ArgumentParser()
parser.add_argument("--write", action="store_true",
                    help="apply changes; without it nothing is written")
args = parser.parse_args()


def load_outline(circuit_key):
    path = os.path.join(CIRCUITS_DIR, "%s.json" % circuit_key)
    if not os.path.exists(path):
        return None
    try:
        return [(p[0], p[1])
                for p in json.loads(io.open(path, encoding="utf-8").read())["outline"]]
    except Exception:
        return None


def span_minutes(s):
    tm = s.get("frame_times") or []
    if len(tm) < 2:
        return 0.0
    a = datetime.datetime.fromisoformat(tm[-1])
    b = datetime.datetime.fromisoformat(tm[0])
    return (a - b).total_seconds() / 60


def main():
    files = sorted(glob.glob(os.path.join(SESSIONS_DIR, "*.json")))
    print("сессий: %d | режим: %s" % (len(files), "запись" if args.write else "проба"))
    print()

    trimmed = repaired = checked = failed = 0

    for path in files:
        name = os.path.basename(path)
        s = json.loads(io.open(path, encoding="utf-8").read())
        frames = s.get("frames") or []
        times = s.get("frame_times") or []
        if len(frames) < 10 or len(times) != len(frames):
            continue

        label = "%-20s %-18s" % (s["circuit_short_name"][:20], s["session_name"][:18])
        changed = False

        # 1. Drop the frames where the field is still parked in the pits.
        outline = load_outline(s.get("circuit_key"))
        if outline:
            t = [datetime.datetime.fromisoformat(x) for x in times]
            nt, nf = ul.trim_pre_session(t, frames, outline)
            if len(nf) < len(frames) and len(nf) >= 10:
                print("  %s обрезка %d -> %d кадров (%d лишних)"
                      % (label, len(frames), len(nf), len(frames) - len(nf)))
                s["frames"] = nf
                s["frame_times"] = [x.isoformat() for x in nt]
                times = s["frame_times"]
                changed = True
                trimmed += 1

        # 2. Put every position event back on the grid it belongs to.
        #
        # Any event pointing past the last frame, or sitting implausibly late in
        # a session that should have overtakes from the first lap, is evidence of
        # the old grid. Refetching is cheap and removes the guesswork.
        n_frames = len(s["frames"])
        ev = s.get("position_events") or []
        suspicious = bool(ev) and (max(e[0] for e in ev) >= n_frames
                                   or min(e[0] for e in ev) > 20)
        if suspicious:
            key = s.get("session_key")
            if key is None:
                print("  %s есть события вне сетки, но нет session_key — пропускаю" % label)
                failed += 1
                continue
            rows = ul.of1_get("position?session_key=%s" % key, retries=4, retry_429=True)
            if rows is None:
                print("  %s OpenF1 не отдал position — пропускаю, в следующем проходе"
                      % label)
                failed += 1
                continue
            drivers = s.get("drivers") or []
            # The stored driver record carries its number as "n" — reading
            # "number" yields None for every driver and the remap silently
            # comes back empty.
            numbers = [d.get("n") for d in drivers]
            if not any(n is not None for n in numbers):
                print("  %s в drivers нет номеров — пропускаю" % label)
                failed += 1
                continue
            fresh = ul.build_position_events(rows, numbers, times)
            if len(fresh) != len(ev) or fresh != ev:
                print("  %s события %d -> %d, индексы пересчитаны по времени"
                      % (label, len(ev), len(fresh)))
                s["position_events"] = fresh
                changed = True
                repaired += 1
            else:
                print("  %s события %d — сетка уже верная" % (label, len(ev)))
            checked += 1
            ul.time.sleep(ul.API_PAUSE)

        if changed and args.write:
            io.open(path, "w", encoding="utf-8", newline="").write(
                json.dumps(s, ensure_ascii=False, separators=(",", ":")))

    print()
    print("обрезано: %d | индексы пересчитаны: %d | проверено: %d | не удалось: %d"
          % (trimmed, repaired, checked, failed))
    if not args.write:
        print("проба. Повторить с --write, чтобы применить.")


if __name__ == "__main__":
    main()