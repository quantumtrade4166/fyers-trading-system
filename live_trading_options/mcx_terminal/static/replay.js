/* MCX Desk — replay player. Loaded BEFORE app.js inside a day's replay file (MCX_YYYY-MM-DD.html).
   It replaces the engine connection with a ReplaySocket that feeds the RECORDED engine messages to the
   unchanged screen code (app.js) at their recorded times, and moves the screen's clock with the replay.
   Same code + same messages = the same screen. Orders and settings are disabled in a replay. */
"use strict";

const RP = {
  ready: false, playing: false, speed: 1, T: 0, t0: 0, t1: 0, wall: 0,
  n: 0, tt: null, ti: null, cols: [], toks: [], i: 0,           // ticks (columnar)
  ev: [], e: 0,                                                   // events [t_off, kind, payload]
  state: new Map(), defs: new Map(), mtm: [], book: null, risk: null, status: null,
  marks: [], sock: null, hdr: null,
};

// ── the screen's clock follows the replay ──────────────────────────────────
const RealDate = Date;
class ReplayDate extends RealDate {
  constructor(...a) { if (a.length === 0) super(RP.ready ? RP.T : RealDate.now()); else super(...a); }
  static now() { return RP.ready ? RP.T : RealDate.now(); }
}
window.Date = ReplayDate;

// ── decode the embedded day ────────────────────────────────────────────────
async function rpBytes(id) {
  const b64 = document.getElementById(id).textContent.trim();
  const bin = atob(b64);
  const u8 = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).arrayBuffer();
}

async function rpLoad() {
  const info = JSON.parse(document.getElementById("rp-info").textContent);
  const buf = await rpBytes("rp-ticks");
  const n = info.n;
  RP.n = n; RP.t0 = info.t0; RP.t1 = info.t1; RP.toks = info.tokens;
  RP.tt = new Uint32Array(buf.slice(0, 4 * n));
  RP.ti = new Uint16Array(buf.slice(4 * n, 6 * n));
  let off = 6 * n;
  for (let j = 0; j < info.fields; j++) { RP.cols.push(new Float32Array(buf.slice(off, off + 4 * n))); off += 4 * n; }
  RP.lastAtStart = info.last;
  RP.ev = JSON.parse(new TextDecoder().decode(await rpBytes("rp-events")));
  RP.hdr = (RP.ev.find((e) => e[1] === "header") || [0, "header", {}])[2];
  // trade markers: the moment each order first appears
  const seen = new Set();
  for (const [t, k, p] of RP.ev) {
    if (k !== "book") continue;
    for (const o of p.orders) if (!seen.has(o.id)) {
      seen.add(o.id);
      RP.marks.push({ t, side: o.side, sym: o.symbol, lots: o.qty / o.lot, src: o.source || "" });
    }
  }
  RP.marks.sort((a, b) => a.t - b.t);
  // open on the instrument + expiry of the first order
  const first = RP.ev.find((e) => e[1] === "book" && e[2].orders.length);
  if (first) {
    const o = first[2].orders[first[2].orders.length - 1];
    try {
      localStorage.setItem("mcxdesk.inst", JSON.stringify(o.name));
      const d = [...(RP.hdr.chains || []), ...RP.ev.filter((e) => e[1] === "chain").map((e) => e[2])]
        .find((c) => c.rows.some((r) => r.ce === o.token || r.pe === o.token));
      if (d) localStorage.setItem("mcxdesk.exp." + o.name, JSON.stringify(d.expiry));
    } catch { /* private mode */ }
  }
  RP.ready = true;
  rpSeek(0);
}

const tickRow = (k) => {
  const r = [RP.toks[RP.ti[k]]];
  for (let j = 0; j < RP.cols.length; j++) {
    const v = RP.cols[j][k];
    r.push(Number.isNaN(v) ? null : j === 9 ? v + Math.floor(RP.t0 / 1000) : v);
  }
  return r;
};
const merge = (row) => {
  const cur = RP.state.get(row[0]) || [row[0], null, null, null, null, null, null, null, null, null, null, null];
  for (let i = 1; i < 12; i++) if (row[i] != null) cur[i] = row[i];
  RP.state.set(row[0], cur);
  return cur;
};

// ── engine messages, as the live engine would have sent them ───────────────
function rpMeta() {
  const m = JSON.parse(JSON.stringify(RP.hdr.meta || {}));
  const recorded = new Map();
  for (const d of [...RP.defs.values()]) {
    if (!recorded.has(d.name)) recorded.set(d.name, new Set());
    recorded.get(d.name).add(d.expiry);
  }
  m.products = (m.products || []).filter((p) => recorded.has(p.name))
    .map((p) => ({ ...p, expiries: p.expiries.filter((e) => recorded.get(p.name).has(e)) }));
  m.mode = "REPLAY";
  if (RP.hdr.settings) m.settings = RP.hdr.settings;
  return m;
}
function emit(obj) { if (RP.sock && RP.sock.onmessage) RP.sock.onmessage({ data: JSON.stringify(obj) }); }
function rpStatus(st) { emit({ ...(st || RP.hdr.status || { t: "status", feed: {} }), t: "status", mode: "REPLAY" }); }

function applyEvent(kind, p, live) {
  if (kind === "chain") RP.defs.set(p.name + "|" + p.expiry, p);
  else if (kind === "book") { RP.book = p; if (live) emit(p); }
  else if (kind === "risk") { RP.risk = p; if (live) emit(p); }
  else if (kind === "status") { RP.status = p; if (live) rpStatus(p); }
  else if (kind === "settings") { if (typeof S !== "undefined") S.settings = p; }
  else if (kind === "mtm") RP.mtm.push(p);
  else if (kind === "atm") { RP.atm = p; if (live) emit(p); }
}

// rebuild everything as it was at offset `off` (ms from the start), then repaint
function rpSeek(off) {
  off = Math.max(0, Math.min(off, RP.t1 - RP.t0));
  RP.state = new Map();
  for (const r of RP.lastAtStart) RP.state.set(r[0], [...r]);
  RP.defs = new Map();
  for (const d of RP.hdr.chains || []) RP.defs.set(d.name + "|" + d.expiry, d);
  RP.mtm = (RP.hdr.mtm || []).filter((p) => p[0] * 1000 <= RP.t0 + off);
  RP.book = { t: "book", positions: (RP.hdr.book && RP.hdr.book.positions || []).filter((p) => p.carried), orders: [] };
  RP.risk = RP.hdr.risk || null;
  RP.atm = null;
  RP.status = RP.hdr.status || null;
  let k = 0;
  while (k < RP.n && RP.tt[k] <= off) { merge(tickRow(k)); k++; }
  RP.i = k;
  let e = 0;
  while (e < RP.ev.length && RP.ev[e][0] <= off) { if (RP.ev[e][1] !== "header") applyEvent(RP.ev[e][1], RP.ev[e][2], false); e++; }
  RP.e = e;
  RP.T = RP.t0 + off;
  if (RP.sock && RP.sock.readyState === 1) {
    emit({ t: "d", s: RP.T / 1000, u: [...RP.state.values()] });
    if (RP.book) emit(RP.book);
    if (RP.risk) emit(RP.risk);
    if (RP.atm) emit(RP.atm);
    rpStatus(RP.status);
  }
  rpPaintBar();
}

// one playback step: advance the clock, deliver ticks (latest per contract) and events in order
function rpStep() {
  if (!RP.ready) return;
  const now = performance.now();
  if (RP.playing) {
    RP.T = Math.min(RP.t1, RP.T + (now - RP.wall) * RP.speed);
    if (RP.T >= RP.t1) RP.playing = false;
  }
  RP.wall = now;
  const off = RP.T - RP.t0;
  const out = new Map();
  while (RP.i < RP.n && RP.tt[RP.i] <= off) { const c = merge(tickRow(RP.i)); out.set(c[0], [...c]); RP.i++; }
  if (out.size) emit({ t: "d", s: RP.T / 1000, u: [...out.values()] });
  while (RP.e < RP.ev.length && RP.ev[RP.e][0] <= off) {
    const [, kind, p] = RP.ev[RP.e];
    if (kind !== "header") applyEvent(kind, p, true);
    RP.e++;
  }
  rpPaintBar();
}
setInterval(rpStep, 40);

// ── the fake engine connection the screen code talks to ────────────────────
class ReplaySocket {
  constructor() {
    this.readyState = 0;
    RP.sock = this;
    const go = () => {
      this.readyState = 1;
      if (this.onopen) this.onopen();
      emit(rpMeta());
      rpStatus(RP.status);
      if (RP.book) emit(RP.book);
      if (RP.risk) emit(RP.risk);
      emit({ t: "d", s: RP.T / 1000, u: [...RP.state.values()] });
    };
    const wait = () => (RP.ready ? go() : setTimeout(wait, 50));
    wait();
  }
  send(txt) {
    const m = JSON.parse(txt);
    if (m.op === "ping") emit({ t: "pong", c: m.c, s: RP.T / 1000 });
    else if (m.op === "chain") {
      let d = RP.defs.get(m.name + "|" + m.expiry) || [...RP.defs.values()].find((x) => x.name === m.name);
      if (!d) return emit({ t: "ack", op: "chain", ok: false, msg: "this chain was not open that day" });
      const toks = d.rows.flatMap((r) => [r.ce, r.pe]).concat(d.fut ? [d.fut.token] : []);
      emit({ ...d, t: "chain", snap: toks.filter((t) => RP.state.has(t)).map((t) => [...RP.state.get(t)]) });
      emit({ t: "d", s: RP.T / 1000, u: toks.filter((t) => RP.state.has(t)).map((t) => [...RP.state.get(t)]) });
    } else if (m.op === "mtm_hist") {
      const step = Math.max(+m.mins || 1, 0.1) * 60, inst = m.inst || "all";
      const pts = RP.mtm.filter((p) => p[0] * 1000 <= RP.T);
      const names = [...new Set(pts.flatMap((p) => Object.keys(p[2] || {})))].sort();
      const kind = m.kind || "pnl";
      const out = []; let lb = null;
      for (const pt of pts) {
        const [t, tot, per] = pt;
        let row;
        if (kind === "imb") {
          const x = pt[3] || {};
          if (x.imb == null) continue;
          row = [t, x.imb, x.ce, x.pe];
        } else {
          const v = inst === "all" ? tot : (per || {})[inst];
          if (v == null) continue;
          row = [t, v];
        }
        const b = Math.floor(t / step);
        if (b === lb) out[out.length - 1] = row; else { out.push(row); lb = b; }
      }
      emit({ t: "ack", op: "mtm_hist", ok: true, quiet: true, tag: m.tag, kind, points: out, instruments: names, mins: m.mins, inst });
    } else if (m.op === "atm_hist") {
      const step = Math.max(+m.mins || 1, 0.1) * 60, off = RP.T - RP.t0;
      const out = []; let lb = null;
      for (const [t, k, p] of RP.ev) {
        if (t > off) break;
        if (k !== "atm" || !p.v || !p.v[m.name]) continue;
        const ts = (RP.t0 + t) / 1000, [strike, v] = p.v[m.name];
        const b = Math.floor(ts / step);
        if (b === lb) out[out.length - 1] = [ts, v, strike]; else { out.push([ts, v, strike]); lb = b; }
      }
      emit({ t: "ack", op: "atm_hist", ok: true, quiet: true, name: m.name, mins: m.mins, points: out });
    } else if (m.op === "margin") {
      emit({ t: "ack", op: "margin", ok: false, msg: "not recorded in replays" });
    } else emit({ t: "ack", op: m.op, ok: false, msg: "Replay: orders and settings are switched off" });
  }
  close() { this.readyState = 3; }
}
window.WebSocket = ReplaySocket;

// ── controls: play / pause / speed / timeline with a marker at every order ─
function rpFmt(ms) {
  return new RealDate(ms).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false });
}
function rpBuildBar() {
  const bar = document.createElement("div");
  bar.id = "rp-bar";
  bar.innerHTML = `
    <span class="rp-tag">REPLAY</span>
    <button id="rp-start" title="Back to the start">⏮</button>
    <button id="rp-prev" title="Previous order (,)">◀ order</button>
    <button id="rp-play" title="Play / pause (space)">▶</button>
    <button id="rp-next" title="Next order (.)">order ▶</button>
    <select id="rp-speed" title="Speed">${[1, 2, 5, 10, 30, 60, 300].map((s) => `<option value="${s}">${s}×</option>`).join("")}</select>
    <span id="rp-time" class="mono"></span>
    <div class="rp-track" id="rp-track"><input type="range" id="rp-slider" min="0" max="1000" step="1" value="0"><div id="rp-marks"></div></div>
    <span id="rp-end" class="mono"></span>`;
  document.body.insertBefore(bar, document.querySelector("footer"));
  const span = () => RP.t1 - RP.t0 || 1;
  document.getElementById("rp-play").onclick = () => { RP.playing = !RP.playing; RP.wall = performance.now(); rpPaintBar(); };
  document.getElementById("rp-start").onclick = () => { RP.playing = false; rpSeek(0); };
  document.getElementById("rp-speed").onchange = (e) => { RP.speed = +e.target.value; };
  document.getElementById("rp-slider").oninput = (e) => rpSeek((+e.target.value / 1000) * span());
  const jump = (dir) => {
    const off = RP.T - RP.t0;
    // we park 1 s before an order, so "next" must look past the order we are parked in front of
    const m = dir > 0 ? RP.marks.find((x) => x.t > off + 1500) : [...RP.marks].reverse().find((x) => x.t < off - 500);
    if (m) rpSeek(Math.max(0, m.t - 1000));                     // land just before the order
  };
  document.getElementById("rp-next").onclick = () => jump(1);
  document.getElementById("rp-prev").onclick = () => jump(-1);
  document.addEventListener("keydown", (e) => {
    const tg = e.target && e.target.tagName;
    if (tg === "INPUT" || tg === "SELECT" || tg === "TEXTAREA") return;
    if (e.code === "Space") { e.preventDefault(); document.getElementById("rp-play").click(); }
    else if (e.key === ".") jump(1);
    else if (e.key === ",") jump(-1);
  });
  const marks = document.getElementById("rp-marks");
  marks.innerHTML = RP.marks.map((m) => `<i class="rp-m ${m.side === "B" ? "b" : "s"}" style="left:${(m.t / span()) * 100}%"
    title="${rpFmt(RP.t0 + m.t)} ${m.side === "B" ? "BUY" : "SELL"} ${m.lots} lot(s) ${m.sym}${m.src ? " · " + m.src : ""}"></i>`).join("");
  document.getElementById("rp-end").textContent = rpFmt(RP.t1);
  document.title = `MCX Desk replay ${new RealDate(RP.t0).toLocaleDateString("en-GB", { timeZone: "Asia/Kolkata" })}`;
}
function rpPaintBar() {
  const el = document.getElementById("rp-time");
  if (!el) return;
  el.textContent = rpFmt(RP.T);
  document.getElementById("rp-play").textContent = RP.playing ? "⏸" : "▶";
  const s = document.getElementById("rp-slider");
  if (document.activeElement !== s) s.value = Math.round(((RP.T - RP.t0) / (RP.t1 - RP.t0 || 1)) * 1000);
}

window.addEventListener("load", () => {
  const rb = document.getElementById("btn-replays");
  if (rb) rb.style.display = "none";                          // no server inside a replay file
  const waitReady = () => (RP.ready ? rpBuildBar() : setTimeout(waitReady, 50));
  waitReady();
  // the feed pill says REPLAY, never LIVE
  if (typeof window.setFeed === "function") {
    const orig = window.setFeed;
    window.setFeed = (txt, cls) => orig(String(txt).replace("LIVE", "REPLAY"), cls);
  }
});
rpLoad().catch((err) => { document.body.insertAdjacentHTML("afterbegin", `<pre style="color:#f55">Replay failed to load: ${err}</pre>`); });
