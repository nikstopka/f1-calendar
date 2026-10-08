"""Check the countdown against the real season file.

The countdown functions are lifted out of index.html verbatim, so this tests the
shipped code rather than a copy of it. Run it from the repository root:

    python scripts/check_countdown.py

Probes are generated from the season file rather than hand-written: one for every
session in the championship, a minute before it starts. That way the check covers
both weekend formats and all four colours without anyone guessing a schedule.
"""
import io, json, os, subprocess, sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

html = io.open("index.html", encoding="utf-8").read()
data = json.loads(io.open("data/seasons/2026.json", encoding="utf-8").read())

start = html.index("const SESSION_STYLE = {")
end = html.index("function startCountdown() {")
body = html[start:end]


def fn(src, name):
    i = src.index(name)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise ValueError(name)


# One probe a minute before each session starts, plus one a minute before it
# ends (which must still be that same session).
import datetime


def iso_minus(iso, seconds):
    t = datetime.datetime.fromisoformat(iso)
    t -= datetime.timedelta(seconds=seconds)
    return t.isoformat()


probes = []
for race in data["races"]:
    for s in race["sessions"]:
        t0 = s["utc_start"]
        probes.append({"iso": iso_minus(t0, 60), "want": s["name"], "why": "перед стартом"})
        t1 = s.get("utc_end") or t0
        probes.append({"iso": iso_minus(t1, 60), "want": s["name"], "why": "во время сессии"})


io.open("_cd.js", "w", encoding="utf-8", newline="\n").write(
    "const seasonData = %s;\n" % json.dumps(data, ensure_ascii=False) + body
    + "\n" + fn(body, "function nextSession")
    + "\nmodule.exports = { SESSION_STYLE, nextSession };\n")
io.open("_pr.json", "w", encoding="utf-8", newline="\n").write(json.dumps(probes))

script = """
const M = require('./_cd.js');
const probes = require('./_pr.json');
const seen = {};
let bad = 0;
for (const p of probes) {
  const n = M.nextSession(new Date(p.iso));
  const got = n ? n.session.name : 'нет';
  const style = n ? (M.SESSION_STYLE[n.session.name] || M.SESSION_STYLE.Race) : null;
  seen[got] = style ? style.color : '-';
  if (got !== p.want) {
    bad++;
    if (bad <= 8) console.log('ПРОВАЛ ' + p.iso + ' (' + p.why + '): ждали '
      + p.want + ', получили ' + got);
  }
}
console.log('проб: ' + probes.length + ' | провалов: ' + bad);
console.log('цвета по сессиям:');
for (const k of Object.keys(seen).sort()) {
  console.log('   ' + k.padEnd(18) + seen[k]);
}
"""
out = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                     encoding="utf-8", errors="replace")
print(out.stdout or out.stderr)
for tmp in ("_cd.js", "_pr.json"):
    if os.path.exists(tmp):
        os.remove(tmp)