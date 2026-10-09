"""One command to check whether real-time F1 data is reachable right now.

Answers two separate questions, because they fail for unrelated reasons:

  1. F1's live timing feed (WebSocket on livetiming.formula1.com). This is what
     gives sub-second telemetry during a session.
  2. OpenF1's API. This is the delayed fallback, and it deliberately locks all
     access — including past sessions — while a session is running.

Run it during a session and again after one ends; the difference tells you
whether a block is tied to the session or is standing.

    python scripts/check_live_sources.py
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

UA = "F1-Calendar-Bot/1.1 (diagnostic; one request per source)"

print("время проверки: %s UTC" % time.strftime("%Y-%m-%d %H:%M", time.gmtime()))
print()

# ── 1. F1 live timing feed ────────────────────────────────────────────────────
print("── лента реального времени F1 ──")
ok = False
try:
    import websocket
    ws = websocket.create_connection("wss://livetiming.formula1.com/signalrcore",
                                     timeout=15, suppress_origin=True,
                                     header=["User-Agent: " + UA])
    ws.send(json.dumps({"protocol": "json", "version": 1}) + "\x1e")
    ws.settimeout(15)
    ws.recv()
    ok = True
    print("   WebSocket: СОЕДИНЁН")
    ws.close()
except Exception as exc:
    print("   WebSocket: ОТКАЗ — %s" % type(exc).__name__)
    m = re.search(r"status (\d+)", str(exc))
    if m:
        print("              HTTP %s от ленты F1" % m.group(1))

# HTTP endpoint of the same host, to tell a socket problem from a host block
try:
    req = urllib.request.Request(
        "https://livetiming.formula1.com/api/session-info",
        headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        print("   HTTP того же хоста: %s" % r.status)
except urllib.error.HTTPError as exc:
    print("   HTTP того же хоста: %s (server=%s)"
          % (exc.code, exc.headers.get("server") if exc.headers else "?"))
except Exception as exc:
    print("   HTTP того же хоста: сбой %s" % type(exc).__name__)

print()

# ── 2. OpenF1 ────────────────────────────────────────────────────────────────
print("── OpenF1 (отложенные данные) ──")
try:
    req = urllib.request.Request(
        "https://api.openf1.org/v1/sessions?year=2026",
        headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())
        print("   сессий в каталоге: %d — ДОСТУПЕН" % len(data))
        now = time.time()
        import datetime as dt
        recent = []
        for s in data:
            t = s.get("date_start")
            if not t:
                continue
            when = dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            if 0 < (when - now) < 6 * 3600 or -6 * 3600 < (when - now) < 0:
                recent.append((s.get("name"), s.get("session_key")))
        if recent:
            print("   идущая или недавняя сессия: %s" % recent[:3])
except urllib.error.HTTPError as exc:
    body = exc.read()[:200].decode("utf-8", "replace")
    print("   ОТКАЗ — HTTP %s" % exc.code)
    print("   %s" % body.strip())
except Exception as exc:
    print("   сбой %s: %s" % (type(exc).__name__, exc))

print()
print("итог: реальное время %s" % ("ЕСТЬ" if ok else "НЕДОСТУПНО"))