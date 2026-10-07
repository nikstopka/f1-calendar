import io, sys, importlib.util, datetime, json, glob, os
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
spec = importlib.util.spec_from_file_location("ul", "scripts/update_live_data.py")
ul = importlib.util.module_from_spec(spec); spec.loader.exec_module(ul)
sess = ul.of1_get("sessions") or []
now = datetime.datetime.now(datetime.timezone.utc)
eligible = [s for s in sess if s.get("year", 0) >= ul.MIN_YEAR
            and s.get("date_end") and ul.parse_dt(s["date_end"]) < now]
ul.ensure_circuits(eligible)
print()
have = {int(os.path.basename(p)[:-5]) for p in glob.glob("data/live/circuits/*.json")}
idx = json.loads(io.open("data/live/index.json", encoding="utf-8").read())
want = set(idx.get("circuits") or [])
print("контуров: %d | трасс в индексе: %d | без файла: %s"
      % (len(have), len(want), sorted(want - have)))
