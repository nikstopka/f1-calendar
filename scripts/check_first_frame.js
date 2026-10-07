// Runs the shipped firstFrameOnTrack() from index.html against every stored
// session, so the display fix is checked on real data rather than on one circuit.
//
// The function is lifted out of index.html verbatim — if it is edited there and
// not here, this still tests the real thing.
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync("index.html", "utf8");
const start = html.indexOf("function firstFrameOnTrack() {");
if (start < 0) throw new Error("firstFrameOnTrack not found in index.html");
let depth = 0, i = html.indexOf("{", start), end = -1;
for (let k = i; k < html.length; k++) {
  if (html[k] === "{") depth++;
  else if (html[k] === "}") { depth--; if (depth === 0) { end = k + 1; break; } }
}
const src = html.slice(start, end);

let liveSession = null, liveCircuit = null;
const fn = new Function("liveSession", "liveCircuit",
  src + "\nreturn firstFrameOnTrack();");
const pick = (s, c) => fn(s, c);

const dir = "data/live/sessions";
const files = fs.readdirSync(dir).filter(f => f.endsWith(".json"));
const circuits = {};
for (const f of fs.readdirSync("data/live/circuits")) {
  if (f.endsWith(".json")) circuits[f.replace(".json", "")] =
    JSON.parse(fs.readFileSync(path.join("data/live/circuits", f), "utf8"));
}

const measure = (f, tr) => {
  const seg = [];
  for (let i2 = 0; i2 < tr.length; i2++) {
    const a = tr[i2], b = tr[(i2 + 1) % tr.length];
    const dx = b[0] - a[0], dy = b[1] - a[1];
    seg.push([a[0], a[1], dx, dy, dx * dx + dy * dy]);
  }
  const ds = [], at = new Set();
  for (let k = 0; k + 1 < f.length; k += 2) {
    if (f[k] < 0 || f[k + 1] < 0) continue;
    at.add(f[k] * 1000000 + f[k + 1]);
    let best = Infinity;
    for (const [x1, y1, dx, dy, L] of seg) {
      let t = L === 0 ? 0 : ((f[k] - x1) * dx + (f[k + 1] - y1) * dy) / L;
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      const ex = f[k] - (x1 + t * dx), ey = f[k + 1] - (y1 + t * dy);
      const d = ex * ex + ey * ey;
      if (d < best) best = d;
    }
    ds.push(best);
  }
  if (!ds.length) return null;
  ds.sort((a, b) => a - b);
  return { dist: Math.sqrt(ds[ds.length >> 1]), spread: at.size / ds.length };
};

let atZero = 0, moved = 0, worst = [];
for (const f of files) {
  const s = JSON.parse(fs.readFileSync(path.join(dir, f), "utf8"));
  const c = circuits[s.circuit_key];
  if (!c || !c.outline) { console.log(`  ${f}: нет контура`); continue; }
  const idx = pick(s, c);
  const m = measure(s.frames[idx], c.outline);
  const m0 = measure(s.frames[0], c.outline);
  if (idx === 0) {
    atZero++;
    console.log(`  0    ${s.circuit_short_name.padEnd(20)} ${s.session_name.padEnd(18)}` +
      ` кадров ${String(s.frames.length).padStart(4)} | 0-й: dist ${m0.dist.toFixed(1)} spread ${m0.spread.toFixed(2)}`);
  } else {
    moved++;
    worst.push(m.dist);
    console.log(`  ${String(idx).padStart(4)} ${s.circuit_short_name.padEnd(20)} ${s.session_name.padEnd(18)}` +
      ` кадров ${String(s.frames.length).padStart(4)} | выбран: dist ${m.dist.toFixed(1)} spread ${m.spread.toFixed(2)}`);
  }
}
worst.sort((a, b) => b - a);
console.log(`\nсессий ${files.length} | старт сдвинут на ${moved} | осталось на 0-м кадре: ${atZero}`);
if (worst.length) console.log(`худшая медиана среди сдвинутых: ${worst[0].toFixed(1)} (порог 25)`);