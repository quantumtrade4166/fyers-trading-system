/* MCX Desk — live option chain + PAPER orders.
   One websocket to the engine. The engine pushes only the ticks this screen watches; we write
   only the cells that changed and flash the price cell green/red.
   Keyboard (when ⌨ Keys is on): arrows move the cursor over the two LTP columns, − sells, + or = buys.
   A held key sends one order only. Orders above the confirm size always ask once more. */
"use strict";

// tick tuple from the engine
const TOK = 0, LTP = 1, BID = 2, ASK = 3, BQ = 4, AQ = 5, VOL = 6, OI = 7, PC = 8, LTQ = 9, LUT = 10, CHG = 11;

const $ = (id) => document.getElementById(id);
const S = {
  ws: null, meta: null, prods: {}, cur: { name: null, expiry: null, strikes: 10 },
  chain: null, T: new Map(), cells: new Map(), rowsByStrike: new Map(), visRows: [],
  atm: null, futTok: null, recv: 0, recvShown: 0, lastTickAt: 0, flip: new Map(),
  book: { positions: [], orders: [] }, sym: new Map(),        // token -> {symbol, lot, side}
  kb: false, askQty: true, askPx: false, qtyLots: 1,
  cursor: { k: null, side: "ce" }, ticket: null, req: 0, ofilter: "all", dockTab: "pos",
};

const nf = new Intl.NumberFormat("en-IN");
const nf2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fp = (v, d = 2) => (v == null || !isFinite(v) ? "—" : v.toFixed(d));
const fq = (v) => (v == null ? "—" : nf.format(v));
const fr = (v) => (v == null || !isFinite(v) ? "—" : Math.abs(v) < 0.005 ? "0.00" : (v > 0 ? "+" : "") + nf2.format(v));
const pct = (l, pc) => (l == null || !pc ? null : ((l - pc) / pc) * 100);
const LS = {
  get(k, d) { try { const v = localStorage.getItem("mcxdesk." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("mcxdesk." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// ── theme (light / dark), remembered on this browser ──
function paintTheme() {
  const light = document.documentElement.dataset.theme === "light";
  $("theme-btn").textContent = light ? "☾" : "☀";
  $("theme-btn").title = light ? "Dark theme" : "Light theme";
}
$("theme-btn").onclick = () => {
  const light = document.documentElement.dataset.theme !== "light";
  if (light) document.documentElement.dataset.theme = "light"; else delete document.documentElement.dataset.theme;
  LS.set("theme", light ? "light" : "dark");
  paintTheme();
  for (const t of ["gp-pnl", "gp-imb"]) if (S.book && S.gpHist && S.gpHist[t]) drawInline(t);   // charts pick up the new colours
};
paintTheme();

// ── connection ──────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  S.ws = ws;
  ws.onopen = () => setFeed("connected", "ok");
  ws.onclose = () => { setFeed("reconnecting…", "bad"); setTimeout(connect, 1000); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    switch (m.t) {
      case "d": onTicks(m.u); break;
      case "meta": onMeta(m); break;
      case "chain": onChain(m); break;
      case "status": onStatus(m); break;
      case "book": onBook(m); break;
      case "atm": onAtm(m.v || {}); break;
      case "risk": onRisk(m); break;
      case "timers": onTimers(m.items || []); break;
      case "note": toast(m.msg, !m.ok, m.ok ? "ok" : "bad"); break;    // a live fill / reject after the pop-up
      case "ack":
        if (m.op === "mtm_hist" && (m.tag === "gp-pnl" || m.tag === "gp-imb")) onInlineGraph(m);
        else if (m.op === "mtm_hist") onMtm(m);
        else if (m.op === "margin") onMargin(m);
        else if (m.op === "atm_hist") onAtmHist(m);
        else if (m.op === "sl_apply") {
          if (m.settings) { S.settings = m.settings; paintSlBar(); }
          toast(m.msg, !m.ok, m.ok ? "ok" : "bad");
        }
        else if (m.op === "settings_set" && m.tag === "slbar") {
          if (m.settings) S.settings = m.settings;
          paintSlBar();
          toast(m.ok ? "auto stop-loss settings saved" : m.msg, !m.ok, m.ok ? "info" : "bad");
        }
        else if (m.op === "settings_set") { onSettingsAck(m); toast(m.msg, !m.ok); }
        else if (m.op === "refresh") { $("btn-refresh").classList.remove("spin"); if (m.margin) onMargin(m.margin); toast(m.msg, !m.ok, m.ok ? "info" : "bad"); }
        else if (m.op === "timer_set" || m.op === "timer_clear") toast(m.msg, !m.ok, m.ok ? "info" : "bad");
        else if (m.op === "mode_set") toast(m.msg, !m.ok, !m.ok ? "bad" : m.mode === "LIVE" ? "ok" : "info");
        else if (ORDER_ACKS.has(m.op)) {
          toast(m.msg || (m.ok ? "done" : "failed"), !m.ok,
                !m.ok ? "bad" : m.state === "waiting" || m.state === "partial" ? "wait" : "ok", S.pend.get(m.req));
          S.pend.delete(m.req);
        }
        else toast(m.msg || (m.ok ? "done" : "failed"), !m.ok, m.ok ? "info" : "bad");
        break;
      case "pong": $("ping").textContent = `${(performance.now() - m.c).toFixed(0)} ms`; break;
    }
  };
}
const send = (o) => { if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify(o)); return true; } return false; };
setInterval(() => send({ op: "ping", c: performance.now() }), 2000);

function setFeed(txt, cls) { $("feedtxt").textContent = txt; $("feed").className = "pill " + (cls || ""); }

// order pop-up: big + solid colour. kind: "ok" (executed, green) · "wait" (placed, waiting, amber) ·
// "bad" (error, red) · "info" (small, neutral). Click to dismiss.
const working = (o) => o.status === "OPEN" || o.status === "SENDING";
function toast(msg, bad, kind, el) {
  kind = kind || (bad ? "bad" : "info");
  const fresh = !el || !el.isConnected;
  if (fresh) el = document.createElement("div");
  el.className = "toast " + kind;
  const head = { ok: "✔ EXECUTED", wait: "⏳ PLACED · WAITING", bad: "✖ ERROR", send: "➤ SENDING" }[kind];
  el.innerHTML = (head ? `<b class="t-h">${head}</b>` : "") + `<span class="t-m"></span>`;
  el.querySelector(".t-m").textContent = msg;
  if (fresh) { el.onclick = () => el.remove(); $("toasts").appendChild(el); }
  clearTimeout(el._t);
  el._t = setTimeout(() => el.remove(), { bad: 8000, wait: 5000, ok: 4000, send: 12000 }[kind] || 3000);
  return el;
}
// an order's pop-up appears the moment it is sent, then turns green / amber / red in the same box
S.pend = new Map();
function pendToast(req, text) { S.pend.set(req, toast(text, false, "send")); }
const ORDER_ACKS = new Set(["order", "basket", "exit", "exit_all", "exit_inst", "move"]);
const _once = {};
function toastOnce(key, msg, bad) { const n = Date.now(); if (!_once[key] || n - _once[key] > 30000) { _once[key] = n; toast(msg, bad); } }

// PAPER / LIVE switch (the engine only accepts LIVE from the owner, with the typed word LIVE)
function toggleMode() {
  if (S.mode === "LIVE") {
    if (confirm("Switch back to PAPER?\n\nNew orders will be paper. Open LIVE positions stay open on Kotak P (switch back to LIVE to see / exit them)."))
      send({ op: "mode_set", mode: "PAPER" });
    return;
  }
  const w = prompt("ARM LIVE TRADING on Kotak P?\n\nEvery order from this screen will be a REAL order on Kotak P (MCX).\nType LIVE to arm:");
  if (w === null) return;
  send({ op: "mode_set", mode: "LIVE", confirm: w });
}
document.getElementById("mode").onclick = toggleMode;

// ── meta / controls ─────────────────────────────────────────────────────────
function onMeta(m) {
  S.meta = m;
  setTimeout(paintSlBar, 0);
  m.products.forEach((p) => (S.prods[p.name] = p));
  const name = S.cur.name || LS.get("inst", m.products[0].name);
  S.cur.strikes = LS.get("strikes", m.default_strikes);
  $("strikes").innerHTML = m.strikes.map((n) => `<option value="${n}">${n}</option>`).join("");
  $("strikes").value = S.cur.strikes;
  // one instrument switch: the button shows the current one, the list shows all with live prices
  $("tickers").innerHTML = m.products.map((p) => `
    <div class="tk" data-n="${p.name}">
      <span class="n">${p.label.toUpperCase()}</span><span class="p" id="tp-${p.name}">—</span>
      <span class="c" id="tc-${p.name}">—</span><span class="s" id="ts-${p.name}"></span>
      <span class="a" id="ta-${p.name}" title="ATM straddle (call + put), nearest expiry"></span>
      <span class="e">${expLine(p)}</span>
    </div>`).join("");
  $("ip-menu").innerHTML = m.products.map((p) => `
    <div class="ip-item" data-n="${p.name}">
      <span class="n">${p.label.toUpperCase()}</span><span class="p mono" id="mp-${p.name}">—</span>
      <span class="c mono" id="mc-${p.name}"></span><span class="s mono" id="ms-${p.name}"></span>
      <span class="a mono" id="ma-${p.name}"></span>
      <span class="e mono">${expLine(p)}</span>
    </div>`).join("");
  $("cp-inst").innerHTML = m.products.map((p) => `<option value="${p.name}">${p.label}</option>`).join("");
  $("cp-inst").onchange = () => paintPnl();
  $("qty").innerHTML = Array.from({ length: m.qty_lots_max }, (_, i) => `<option value="${i + 1}">${i + 1} lot${i ? "s" : ""}</option>`).join("");
  document.querySelectorAll(".ip-item, .tk").forEach((b) => (b.onclick = () => { pick(b.dataset.n); ipClose(); }));
  pick(S.prods[name] ? name : m.products[0].name, true);
}

function underlyingFut(p, expiry) { return p.futures.find((f) => f.expiry >= expiry) || p.futures[p.futures.length - 1]; }

function pick(name, keepExpiry) {
  const p = S.prods[name];
  S.cur.name = name;
  LS.set("inst", name);
  const saved = keepExpiry ? LS.get("exp." + name, null) : null;
  S.cur.expiry = p.expiries.includes(saved) ? saved : p.expiries[0];
  $("expiry").innerHTML = p.expiries.map((e) => `<option value="${e}">${fmtDate(e)} · ${daysLeft(e)}</option>`).join("");
  $("expiry").value = S.cur.expiry;
  document.querySelectorAll(".ip-item, .tk").forEach((b) => b.classList.toggle("on", b.dataset.n === name));
  $("ipb-n").textContent = p.label.toUpperCase();
  $("cp-inst").value = name;
  if (S.book) paintPnl();
  ipMirror();
  S.qtyLots = LS.get("qty." + name, 1);
  paintQty();
  requestChain();
  paintExitBar();
}

function requestChain() {
  $("rows").innerHTML = `<tr><td colspan="11" class="empty">Loading chain…</td></tr>`;
  S.cells.clear(); S.rowsByStrike.clear(); S.atm = null;
  send({ op: "chain", name: S.cur.name, expiry: S.cur.expiry });
}

$("expiry").onchange = (e) => { S.cur.expiry = e.target.value; LS.set("exp." + S.cur.name, S.cur.expiry); requestChain(); };
$("strikes").onchange = (e) => { S.cur.strikes = +e.target.value; LS.set("strikes", S.cur.strikes); layout(); };
$("qty").onchange = (e) => setQty(+e.target.value);
$("qminus").onclick = () => setQty(S.qtyLots - 1);
$("qplus").onclick = () => setQty(S.qtyLots + 1);
function setQty(n) {
  S.qtyLots = Math.max(1, Math.min(S.meta.qty_lots_max, n | 0));
  LS.set("qty." + S.cur.name, S.qtyLots);
  paintQty();
}
function paintQty() {
  $("qty").value = S.qtyLots;
  const p = S.prods[S.cur.name];
  $("qunits").textContent = p ? `= ${fq(S.qtyLots * p.lot)} qty` : "";
}

// toggles
function paintToggles() {
  $("tg-kb").classList.toggle("on", S.kb);
  $("tg-kb").textContent = S.kb ? "⌨ KEYBOARD ON" : "⌨ KEYBOARD OFF";
  $("tg-qty").classList.toggle("on", S.askQty);
  $("tg-px").classList.toggle("on", S.askPx);
  $("f-kb").textContent = S.kb ? "⌨ ↑↓←→ move · − sell · + / = buy" : "";
  paintCursor();
}
S.kb = LS.get("kb", false); S.askQty = LS.get("askQty", true); S.askPx = LS.get("askPx", false);
$("tg-kb").onclick = () => { S.kb = !S.kb; LS.set("kb", S.kb); paintToggles(); };
$("tg-qty").onclick = () => { S.askQty = !S.askQty; LS.set("askQty", S.askQty); paintToggles(); };
$("tg-px").onclick = () => { S.askPx = !S.askPx; LS.set("askPx", S.askPx); paintToggles(); };
paintToggles();

// expiry helpers: days left counted on the IST calendar
function daysLeft(iso) {
  const today = new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
  const d = Math.round((new Date(iso + "T00:00:00Z") - new Date(today + "T00:00:00Z")) / 86400000);
  return d <= 0 ? "today" : d === 1 ? "1 day" : d + " days";
}
const dm = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
function expLine(p) {
  const oe = p.expiries[0], f = oe ? underlyingFut(p, oe) : p.futures[0];
  return (oe ? `OPT ${dm(oe)} (${daysLeft(oe)})` : "") + (f ? ` · FUT ${dm(f.expiry)}` : "");
}
function fmtDate(iso) {
  return new Date(iso + "T00:00:00").toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

// ── chain ───────────────────────────────────────────────────────────────────
function onChain(m) {
  if (m.name !== S.cur.name) return;
  S.chain = m;
  S.cur.expiry = m.expiry;
  S.futTok = m.fut ? m.fut.token : null;
  m.snap.forEach((t) => S.T.set(t[TOK], t));
  m.rows.forEach((r) => {
    S.sym.set(r.ce, { symbol: r.cs, lot: m.lot, k: r.k, side: "ce", name: m.name, ex: m.expiry });
    S.sym.set(r.pe, { symbol: r.ps, lot: m.lot, k: r.k, side: "pe", name: m.name, ex: m.expiry });
  });
  S.atm = null;
  if (S.cursor.k == null) S.cursor = { k: null, side: "ce" };
  layout();
}

const futPrice = () => { const t = S.futTok != null ? S.T.get(S.futTok) : null; return t ? t[LTP] : null; };
function nearestIdx(rows, px) {
  let best = 0, bd = Infinity;
  rows.forEach((r, i) => { const d = Math.abs(r.k - px); if (d < bd) { bd = d; best = i; } });
  return best;
}

const BS = `<span class="bs"><button class="b" data-a="B">B</button><button class="s" data-a="S">S</button></span>`;

// (re)build the visible rows around ATM; called when the chain arrives, strikes change or ATM moves
function layout() {
  const m = S.chain;
  if (!m) return;
  const px = futPrice();
  const rows = gridRows();
  const ai = px == null ? Math.floor(rows.length / 2) : nearestIdx(rows, px);
  const n = S.cur.strikes;
  const vis = rows.slice(Math.max(0, ai - n), ai + n + 1);
  S.visRows = vis;
  S.atm = rows[ai] ? rows[ai].k : null;
  if (S.cursor.k == null || !vis.some((r) => r.k === S.cursor.k)) S.cursor.k = S.atm;
  S.cells.clear(); S.rowsByStrike.clear();
  const tb = document.createDocumentFragment();
  for (const r of vis) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="ba"></td><td class="ba"></td>` +
      `<td class="act" data-tok="${r.ce}">${BS}</td><td class="ltp"></td><td class="pos"></td>` +
      `<td class="k">${fp(r.k, r.k % 1 ? 1 : 0)}</td>` +
      `<td class="pos"></td><td class="ltp"></td><td class="act" data-tok="${r.pe}">${BS}</td>` +
      `<td class="ba"></td><td class="ba"></td>`;
    const td = tr.children;
    S.cells.set(r.ce, { bid: td[0], ask: td[1], ltp: td[3], pos: td[4] });
    S.cells.set(r.pe, { pos: td[6], ltp: td[7], bid: td[9], ask: td[10] });
    td[3].dataset.tok = r.ce; td[7].dataset.tok = r.pe;
    S.rowsByStrike.set(r.k, tr);
    tb.appendChild(tr);
  }
  const body = $("rows");
  body.innerHTML = "";
  body.appendChild(tb);
  for (const r of vis) { paint(r.ce, false); paint(r.pe, false); }
  shade();
  paintCursor();
  markPositions();
  const atr = S.rowsByStrike.get(S.atm);
  if (atr) atr.scrollIntoView({ block: "center" });
  paintUnderlying();
  if (S.view !== "chain") buildCombo();
}

// one click handler for every B / S button in the chain
$("rows").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-a]");
  if (b) { const tok = +b.closest("td").dataset.tok; startOrder(tok, b.dataset.a); return; }
  const ltp = e.target.closest("td.ltp");
  if (ltp && S.kb) { const s = S.sym.get(+ltp.dataset.tok); if (s) { S.cursor = { k: s.k, side: s.side }; paintCursor(); } }
});

function shade() {
  const px = futPrice();
  for (const [k, tr] of S.rowsByStrike) {
    tr.classList.toggle("atm", k === S.atm);
    const c = tr.children;
    const ceItm = px != null && k < px, peItm = px != null && k > px;
    for (let i = 0; i < 5; i++) c[i].classList.toggle("itm", ceItm);
    for (let i = 6; i < 11; i++) c[i].classList.toggle("itm", peItm);
  }
}

function paint(tok, flash, prevLtp) {
  const c = S.cells.get(tok);
  const t = S.T.get(tok);
  if (!c || !t) return;
  const l = t[LTP];
  set(c.ltp, fp(l));
  const ch = pct(l, t[PC]);                         // LTP stays green / red vs the previous close
  const dir = ch == null ? "" : ch > 0 ? "up" : ch < 0 ? "dn" : "";
  c.ltp.classList.toggle("up", dir === "up"); c.ltp.classList.toggle("dn", dir === "dn");
  set(c.bid, fp(t[BID])); set(c.ask, fp(t[ASK]));
  if (flash && prevLtp != null && l != null && l !== prevLtp) {
    const k = (S.flip.get(tok) || 0) ^ 1;
    S.flip.set(tok, k);
    c.ltp.classList.remove("fu1", "fu2", "fd1", "fd2");
    c.ltp.classList.add((l > prevLtp ? "fu" : "fd") + (k ? "1" : "2"));
  }
}
function set(el, txt) { if (el.textContent !== txt) el.textContent = txt; }

// ── keyboard ────────────────────────────────────────────────────────────────
function cursorToken() {
  const r = S.visRows.find((x) => x.k === S.cursor.k);
  return r ? (S.cursor.side === "ce" ? r.ce : r.pe) : null;
}
function paintCursor() {
  document.querySelectorAll("#rows td.cur").forEach((td) => td.classList.remove("cur"));
  if (!S.kb) return;
  const tok = cursorToken();
  const c = tok != null ? S.cells.get(tok) : null;
  if (c) { c.ltp.classList.add("cur"); c.ltp.scrollIntoView({ block: "nearest" }); }
}
document.addEventListener("keydown", (e) => {
  if (S.ticket) return;                                         // the ticket has its own keys
  if (!S.kb || e.ctrlKey || e.metaKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) return;
  if (e.key.startsWith("Arrow")) {
    e.preventDefault();
    const i = S.visRows.findIndex((x) => x.k === S.cursor.k);
    if (e.key === "ArrowLeft") S.cursor.side = "ce";
    else if (e.key === "ArrowRight") S.cursor.side = "pe";
    else if (e.key === "ArrowUp" && i > 0) S.cursor.k = S.visRows[i - 1].k;
    else if (e.key === "ArrowDown" && i < S.visRows.length - 1) S.cursor.k = S.visRows[i + 1].k;
    paintCursor();
    return;
  }
  const buy = e.key === "+" || e.key === "=" || e.code === "NumpadAdd";
  const sell = e.key === "-" || e.code === "NumpadSubtract";
  if (buy || sell) {
    e.preventDefault();
    if (e.repeat) return;                                       // a held key sends one order only
    const tok = cursorToken();
    if (tok != null) startOrder(tok, buy ? "B" : "S");
  }
});

// ── order entry ─────────────────────────────────────────────────────────────
// Every entry point (chain B/S, keyboard, straddle/strangle, Add) builds a list of LEGS [{tok, side}].
// With Hedges on, each SELL leg gets a BUY hedge `hedgeN` strikes further out of the money; the
// engine places a basket buys-first, so the hedge is in before the sell.
function startOrder(tok, side, opts = {}) { startLegs([{ tok, side }], opts); }

function legInfo(tok) { return S.sym.get(tok) || posInfo(tok); }
function posInfo(tok) {
  const p = S.book.positions.find((x) => x.token === tok);
  return p ? { symbol: p.symbol, lot: p.lot } : null;
}

function startLegs(legs, opts = {}) {
  if (legs.some((l) => !legInfo(l.tok))) return toast("unknown contract", true);
  const lots = opts.lots || S.qtyLots;
  const full = withHedges(legs);
  if (!full) return;
  if (!opts.noAsk && (S.askQty || (S.askPx && legs.length === 1) || opts.ticket)) return openTicket(legs, lots);
  if (lots > S.meta.confirm_lots) return openTicket(legs, lots);
  sendLegs(full, lots, null, opts.source || (S.kb ? "keyboard" : "button"));
}

function hedgeTok(tok) {
  const info = S.sym.get(tok);
  if (!info || !S.chain) return null;
  const rows = S.chain.rows, g = gapPts();
  if (g) {                                   // N strikes out = N x the strike gap, in points
    const k = info.k + (info.side === "ce" ? 1 : -1) * S.hedgeN * g;
    const r = rows.find((x) => Math.abs(x.k - k) < 1e-9);
    return r ? (info.side === "ce" ? r.ce : r.pe) : null;
  }
  const i = rows.findIndex((r) => r.k === info.k);
  const j = info.side === "ce" ? i + S.hedgeN : i - S.hedgeN;
  if (i < 0 || j < 0 || j >= rows.length) return null;
  return info.side === "ce" ? rows[j].ce : rows[j].pe;
}

// null = refused (a hedge strike is missing); never send a naked sell when Hedges is on
function withHedges(legs) {
  if (!S.hedge) return legs;
  const out = [...legs];
  for (const l of legs) {
    if (l.side !== "S") continue;
    const h = hedgeTok(l.tok);
    if (h == null) { toast(`no hedge strike ${S.hedgeN} away for ${legInfo(l.tok).symbol} — order not sent`, true); return null; }
    out.push({ tok: h, side: "B", hedge: true });
  }
  return out;
}

function sendLegs(legs, lots, limit, source) {
  if (lots > S.meta.max_lots) return toast(`${lots} lots is over the cap of ${S.meta.max_lots}`, true);
  let ok;
  const req = ++S.req;
  if (legs.length === 1) ok = send({ op: "order", token: legs[0].tok, side: legs[0].side, lots, limit, source, req });
  else ok = send({ op: "basket", legs: legs.map((l) => ({ token: l.tok, side: l.side, lots })), source, req });
  if (!ok) return toast("not connected", true);
  const i = S.sym.get(legs[0].tok) || {};
  const what = legs.length === 1
    ? `${legs[0].side === "B" ? "BUY" : "SELL"} ${lots} lot${lots > 1 ? "s" : ""} ${i.name || ""} ${i.k ?? ""} ${(i.side || "").toUpperCase()}` +
      (limit ? ` @ ${limit}` : "")
    : `basket of ${legs.length} legs × ${lots} lot${lots > 1 ? "s" : ""}`;
  pendToast(req, what + (S.mode === "LIVE" ? " → Kotak" : " (paper)"));
}

function openTicket(legs, lots) {
  const side = legs[0].side;
  const box = $("ticket-box");
  S.ticket = { legs, side, confirmed: false };
  box.className = "ticket " + (side === "B" ? "buy" : "sell");
  $("t-side").textContent = (side === "B" ? "BUY" : "SELL") + (legs.length > 1 ? " ×" + legs.length : "");
  const full = withHedges(legs) || legs;
  $("t-sym").textContent = full.map((l) => (l.hedge ? "+ hedge " : "") + legInfo(l.tok).symbol).join("  ·  ");
  $("t-lots").value = lots;
  const single = legs.length === 1;
  $("t-pxrow").hidden = !(S.askPx && single);
  $("t-px").value = "";
  const t = S.T.get(legs[0].tok);
  if (S.askPx && single && t) $("t-px").placeholder = `empty = at the market (${fp(side === "B" ? t[ASK] : t[BID])})`;
  $("ticket").hidden = false;
  ticketPaint();
  const f = S.askQty || !(S.askPx && single) ? $("t-lots") : $("t-px");
  f.focus(); f.select();
}
function ticketPaint() {
  const k = S.ticket;
  if (!k) return;
  if (k.legs.length === 1) {
    const t = S.T.get(k.legs[0].tok);
    $("t-quote").textContent = t ? `LTP ${fp(t[LTP])} · Bid ${fp(t[BID])} × ${fq(t[BQ])} lots · Ask ${fp(t[ASK])} × ${fq(t[AQ])} lots` : "no quote yet";
  } else {
    const ts = k.legs.map((l) => S.T.get(l.tok));
    const sum = ts.every((t) => t && t[LTP] != null) ? ts.reduce((a, t) => a + t[LTP], 0) : null;
    $("t-quote").textContent = `Combined LTP ${fp(sum)} · ` + ts.map((t) => fp(t ? t[LTP] : null)).join(" + ");
  }
  const lots = Math.max(0, +$("t-lots").value | 0);
  const lot = legInfo(k.legs[0].tok).lot;
  $("t-units").textContent = `= ${fq(lots * lot)} qty${k.legs.length > 1 ? " each" : ""}`;
  let w = "";
  if (lots > S.meta.max_lots) w = `Over the cap of ${S.meta.max_lots} lots — will be refused`;
  else if (lots > S.meta.confirm_lots) w = k.confirmed ? `Press Enter again to send ${lots} lots` : `${lots} lots — press Enter twice to confirm`;
  if (S.hedge && k.legs.some((l) => l.side === "S")) w = (w ? w + " · " : "") + `hedge ${S.hedgeN} strike(s) out is bought first`;
  $("t-warn").textContent = w;
}
function closeTicket() { $("ticket").hidden = true; S.ticket = null; }
function submitTicket() {
  const k = S.ticket;
  const lots = +$("t-lots").value | 0;
  if (lots < 1) return toast("enter at least 1 lot", true);
  if (lots > S.meta.confirm_lots && !k.confirmed) { k.confirmed = true; ticketPaint(); return; }
  const px = S.askPx && k.legs.length === 1 ? parseFloat($("t-px").value) : NaN;
  const full = withHedges(k.legs);
  if (!full) return;
  const limit = isFinite(px) && px > 0 ? px : null;
  if (limit != null && full.length > 1) toast("limit price applies to single orders only — sent at the market with its hedge", true);
  sendLegs(full, lots, full.length === 1 ? limit : null, S.kb ? "keyboard" : "button");
  closeTicket();
}
$("t-go").onclick = submitTicket;
$("t-cancel").onclick = closeTicket;
$("ticket").addEventListener("click", (e) => { if (e.target.id === "ticket") closeTicket(); });
$("t-lots").addEventListener("input", () => { if (S.ticket) { S.ticket.confirmed = false; ticketPaint(); } });
$("ticket").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); if (!e.repeat) submitTicket(); }
  else if (e.key === "Escape") { e.preventDefault(); closeTicket(); }
  else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && e.target.id === "t-lots") {
    e.preventDefault();
    $("t-lots").value = Math.max(1, (+$("t-lots").value | 0) + (e.key === "ArrowUp" ? 1 : -1));
    S.ticket.confirmed = false; ticketPaint();
  }
});

$("exitall").onclick = () => {
  if (send({ op: "exit_all", req: ++S.req }) && S.mode === "LIVE")
    toast("EXIT ALL sent - closing every Kotak MCX position, checking Kotak until flat…", false, "wait");
};
document.querySelectorAll("#sidepane button[data-frac]").forEach((b) =>
  (b.onclick = () => send({ op: "exit_all", frac: +b.dataset.frac, req: ++S.req })));
$("cancelall").onclick = () => send({ op: "cancel_all", req: ++S.req });

// ── ticks ───────────────────────────────────────────────────────────────────
function onTicks(list) {
  if (!list.length) return;
  S.recv += list.length;
  S.lastTickAt = Date.now();
  let futMoved = false, posMoved = false;
  for (const t of list) {
    const tok = t[TOK];
    const prev = S.T.get(tok);
    const prevLtp = prev ? prev[LTP] : null;
    S.T.set(tok, t);
    if (S.cells.has(tok)) paint(tok, true, prevLtp);
    if (tok === S.futTok) futMoved = true;
    if (S.posTok && S.posTok.has(tok)) posMoved = true;
    paintTicker(tok);
  }
  if (futMoved) {
    const m = S.chain, px = futPrice();
    if (m && px != null) {
      const g = gridRows(), k = g[nearestIdx(g, px)].k;
      if (k !== S.atm) layout(); else { shade(); paintUnderlying(); }
    }
  }
  if (posMoved) { paintPnl(); paintRiskSum(); S.gpDirty = true; }
  if (S.view !== "chain") paintCombo(list);
  if (S.ticket) ticketPaint();
}

function paintTicker(tok) {
  for (const p of Object.values(S.prods)) {
    const f = underlyingFut(p, p.expiries[0]);
    if (!f || f.token !== tok) continue;
    const t = S.T.get(tok);
    const el = $("tp-" + p.name);
    const prev = el.dataset.v ? +el.dataset.v : null;
    el.textContent = fp(t[LTP], 0);
    el.dataset.v = t[LTP];
    const ch = pct(t[LTP], t[PC]);
    const c = $("tc-" + p.name);
    c.textContent = ch == null ? "" : `${t[LTP] - t[PC] > 0 ? "+" : ""}${fp(t[LTP] - t[PC], 0)} (${ch.toFixed(2)}%)`;
    c.style.color = ch > 0 ? "var(--up)" : ch < 0 ? "var(--down)" : "var(--muted)";
    if (prev != null && prev !== t[LTP]) el.style.color = t[LTP] > prev ? "var(--up)" : "var(--down)";
    ipMirror();
  }
}

// the switch button repeats the current instrument's price + change from its list row
// the top tickers are hidden (owner, 29-Oct) but still updated: the picker copies its live prices from them
function ipMirror() {
  for (const n of Object.keys(S.prods)) {                // the list rows repeat the top tickers
    const src = $("tp-" + n), c = $("tc-" + n), sy = $("ts-" + n);
    if (!src || !$("mp-" + n)) continue;
    $("mp-" + n).textContent = src.textContent; $("mp-" + n).style.color = src.style.color;
    $("mc-" + n).textContent = c.textContent; $("mc-" + n).style.color = c.style.color;
    $("ms-" + n).textContent = sy.textContent;
  }
  const n = S.cur.name;
  if (!n || !$("tp-" + n)) return;
  const src = $("tp-" + n), c = $("tc-" + n);
  $("ipb-p").textContent = src.textContent;
  $("ipb-p").style.color = src.style.color;
  $("ipb-c").textContent = c.textContent;
  $("ipb-c").style.color = c.style.color;
}
function ipClose() { $("ip-menu").hidden = true; $("ip-btn").classList.remove("open"); }
$("ip-btn").onclick = (e) => {
  e.stopPropagation();
  const open = $("ip-menu").hidden;
  $("ip-menu").hidden = !open;
  $("ip-btn").classList.toggle("open", open);
};
document.addEventListener("click", (e) => { if (!e.target.closest("#instpick")) ipClose(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") ipClose(); });

function paintUnderlying() {
  const m = S.chain;
  if (!m || !m.fut) return;
  const px = futPrice();
  const row = m.rows.find((r) => r.k === S.atm);
  let syn = null;
  if (row) {
    const ce = S.T.get(row.ce), pe = S.T.get(row.pe);
    if (ce && pe && ce[LTP] != null && pe[LTP] != null) syn = row.k + ce[LTP] - pe[LTP];
  }
  $("und").innerHTML = `${m.fut.symbol} <b>${fp(px, 0)}</b> · Syn <b>${fp(syn, 1)}</b> · ATM <b>${S.atm ?? "—"}</b> · Lot ${m.lot}`;
  let str = null;
  if (row) {
    const ce = S.T.get(row.ce), pe = S.T.get(row.pe);
    if (ce && pe && ce[LTP] != null && pe[LTP] != null) str = ce[LTP] + pe[LTP];
  }
  $("ipb-a").textContent = str == null ? "" : `ATM ${S.atm} · ${nf2.format(str)}`;
  const s = $("ts-" + m.name);
  if (s) s.textContent = syn == null ? "" : `Syn ${fp(syn, 0)}`;
}

// ── positions / orders ──────────────────────────────────────────────────────
function onBook(m) {
  S.book = m;
  S.posTok = new Set(m.positions.flatMap((p) => [p.token, ...(p.away ? [p.away.token] : []), ...(p.close ? [p.close.token] : [])]));
  $("npos").textContent = m.positions.filter((p) => p.net).length;
  $("nord").textContent = m.orders.filter(working).length || m.orders.length;
  renderPositions();
  renderOrders();
  markPositions();
}

function posPnl(p) {
  const t = S.T.get(p.token);
  const ltp = t ? t[LTP] : null;
  const open = p.net && ltp != null ? (ltp - p.avg) * p.net * p.mult : 0;
  return { ltp, open, total: p.realized + open };
}

// ── Positions table (Trademate-style): column groups, filters, grouping, per-row SL / Away / Exit / Add ──
const PCOLS = [
  { k: "sel", g: "", h: `<input type="checkbox" id="p-all" title="Select all shown">` },
  { k: "symbol", g: "POSITION", h: "Symbol", f: 1, l: 1 },
  { k: "name", g: "POSITION", h: "Index", f: 1, l: 1 },
  { k: "strike", g: "POSITION", h: "Strike", f: 1 },
  { k: "kind", g: "POSITION", h: "C|P", f: 1 },
  { k: "mny", g: "POSITION", h: "ATM/OTM", f: 1 },
  { k: "expiry", g: "POSITION", h: "Expiry", f: 1 },
  { k: "product", g: "POSITION", h: "Product", f: 1 },
  { k: "net", g: "POSITION", h: "Net qty", f: 1 },
  { k: "avg", g: "POSITION", h: "Avg" },
  { k: "ltp", g: "MARKET", h: "LTP" },
  { k: "pnl", g: "P&L", h: "PNL" },
  { k: "realized", g: "P&L", h: "Realized" },
  { k: "bval", g: "VALUE", h: "Buy val" },
  { k: "sval", g: "VALUE", h: "Sell val" },
  { k: "sl", g: "RISK", h: `<input type="checkbox" id="p-slall" title="Stop-loss on / off for every row shown"> SL` },
  { k: "away", g: "AWAY", h: "" },
  { k: "close", g: "MOVE CLOSE", h: "" },
  { k: "exit", g: "EXIT", h: "" },
  { k: "add", g: "ADD", h: "" },
];
const PGROUPS = ["POSITION", "MARKET", "P&L", "VALUE", "RISK", "AWAY", "MOVE CLOSE", "EXIT", "ADD"];
S.pHide = new Set(LS.get("p.hide", ["realized", "avg"]));
S.pGroup = LS.get("p.group", true);
S.pColl = new Set(LS.get("p.coll", []));
S.pf = {};                      // column -> Set of allowed display values (filters)
S.pin = {};                     // "tok:field" -> what the owner typed (kept across re-renders)
S.pSel = new Set();             // selected rows (tokens)

// future the option is written on, its price, the strike gap -> ATM / OTM n / ITM n (in strike gaps)
function pFutPx(name, expiry) {
  const pr = S.prods[name];
  if (!pr || !pr.futures || !pr.futures.length) return null;
  const f = underlyingFut(pr, expiry);
  const t = f && S.T.get(f.token);
  return t ? t[LTP] : null;
}
function pGap(p) {
  const v = S.settings || (S.meta && S.meta.settings) || {};
  return +((v.strike_gap || {})[p.name]) || (p.away ? Math.abs(p.away.strike - p.strike) : 0);
}
function money(p) {
  if (p.kind !== "CE" && p.kind !== "PE") return "";
  const fut = pFutPx(p.name, p.expiry), g = pGap(p);
  if (fut == null || !g) return "";
  const atm = Math.round(fut / g) * g;
  const n = Math.round((p.strike - atm) / g) * (p.kind === "CE" ? 1 : -1);
  return n === 0 ? "ATM" : n > 0 ? `OTM ${n}` : `ITM ${-n}`;
}
// ladder from the centre: per instrument + expiry, CE strikes high -> low, then PE strikes high -> low, futures last
function ladder(list) {
  const key = (p) => `${p.name}|${p.expiry}`;
  const blocks = new Map();
  for (const p of list) { if (!blocks.has(key(p))) blocks.set(key(p), []); blocks.get(key(p)).push(p); }
  const out = [];
  for (const k of [...blocks.keys()].sort()) {
    const b = blocks.get(k);
    const ce = b.filter((p) => p.kind === "CE").sort((x, y) => y.strike - x.strike);
    const pe = b.filter((p) => p.kind === "PE").sort((x, y) => y.strike - x.strike);
    const rest = b.filter((p) => p.kind !== "CE" && p.kind !== "PE");
    out.push(...ce);
    if (ce.length && pe.length) out.push({ mid: true, name: b[0].name, expiry: b[0].expiry });
    out.push(...pe, ...rest);
  }
  return out;
}
const dmy = (iso) => (iso ? iso.slice(8, 10) + "-" + iso.slice(5, 7) + "-" + iso.slice(0, 4) : "");
const pLots = (p) => p.net / p.lot;
function pVal(p, k) {                                   // the display value a filter works on
  switch (k) {
    case "symbol": return p.kind === "FUT" ? `${p.name} FUT ${dmy(p.expiry)}` : `${p.name} ${fp(p.strike, p.strike % 1 ? 1 : 0)} ${p.kind} ${dmy(p.expiry)}`;
    case "name": return p.name || "";
    case "strike": return p.kind === "FUT" ? "FUT" : fp(p.strike, p.strike % 1 ? 1 : 0);
    case "kind": return p.kind === "FUT" ? "FUT" : p.kind || "";
    case "expiry": return dmy(p.expiry);
    case "product": return p.product || "";
    case "net": return String(pLots(p));
    case "mny": return money(p);
    default: return "";
  }
}
const pCols = () => PCOLS.filter((c) => !S.pHide.has(c.k) && !S.pHide.has(c.g));
function pShown() {
  return S.book.positions.filter((p) => Object.entries(S.pf).every(([k, set]) => !set || set.has(pVal(p, k))));
}

function renderPosHead() {
  const cols = pCols();
  let g1 = "", i = 0;
  while (i < cols.length) {
    let j = i;
    while (j < cols.length && cols[j].g === cols[i].g) j++;
    const g = cols[i].g;
    g1 += `<th colspan="${j - i}" class="pg pg-${g.replace(/[^A-Z]/g, "").toLowerCase() || "x"}">${g}</th>`;
    i = j;
  }
  const g2 = cols.map((c) => `<th class="${c.l ? "l" : ""} pc-${c.k}">${c.h}` +
    (c.f ? ` <button class="pf ${S.pf[c.k] ? "on" : ""}" data-f="${c.k}" title="Filter">⏷</button>` : "") + `</th>`).join("");
  $("pos-head").innerHTML = `<tr class="pgr">${g1}</tr><tr>${g2}</tr>`;
}

function inp(tok, f, def, cls = "pn") {
  const v = S.pin[tok + ":" + f];
  return `<input type="number" class="${cls}" data-in="${f}" min="1" step="1" value="${v != null ? v : def}">`;
}
function pCell(p, k) {
  const tok = p.token, open = !!p.net, opt = p.kind === "CE" || p.kind === "PE";
  switch (k) {
    case "sel": return `<td><input type="checkbox" class="p-sel" ${S.pSel.has(tok) ? "checked" : ""}></td>`;
    case "symbol": return `<td class="l" title="${p.symbol}">${pVal(p, "symbol")}</td>`;
    case "name": case "strike": case "kind": case "expiry": case "product":
      return `<td class="${k === "name" ? "l" : ""} ${k === "kind" ? (p.kind === "CE" ? "cp-ce" : "cp-pe") : ""}">${pVal(p, k)}</td>`;
    case "mny": { const m = money(p); return `<td class="p-mny ${m === "ATM" ? "atm" : m.startsWith("ITM") ? "itm" : ""}">${m}</td>`; }
    case "net": { const n = pLots(p); return `<td class="${n > 0 ? "up-t" : n < 0 ? "dn-t" : ""}">${n ? (n > 0 ? "+" : "") + n : "0"}</td>`; }
    case "avg": return `<td>${open ? fp(p.avg) : "—"}</td>`;
    case "ltp": return `<td class="pl-ltp">—</td>`;
    case "pnl": return `<td class="pl-open">—</td>`;
    case "realized": return `<td class="${p.realized > 0 ? "up-t" : p.realized < 0 ? "dn-t" : ""}">${fr(p.realized)}</td>`;
    case "bval": return `<td>${nf2.format(p.bval || 0)}</td>`;
    case "sval": return `<td>${nf2.format(p.sval || 0)}</td>`;
    case "sl": {
      if (!open || !opt) return `<td class="pc-sl"></td>`;
      const r = (S.book.sl || {})[tok];
      const on = !!r && r.state !== "hit";
      const v = S.pin[tok + ":slpx"] != null ? S.pin[tok + ":slpx"] : r && r.trigger != null ? r.trigger : "";
      const qm = (r && r.qty_mode) || slCfg().qty || "full";
      const st = r ? ({ pending: ["at Kotak", "ok"], watching: ["watched", "warn"], past: ["already past", "bad"], hit: ["HIT", "bad"], new: ["…", ""] }[r.state] || [r.state, ""]) : ["", ""];
      return `<td class="pc-sl"><input type="checkbox" class="p-slon" ${on ? "checked" : ""} title="Stop-loss on / off">` +
        `<input type="number" class="pn sl" data-in="slpx" step="any" value="${v}" placeholder="—" title="Stop-loss trigger price (Enter to apply)">` +
        `<select class="p-slq"><option value="full" ${qm === "full" ? "selected" : ""}>Full</option><option value="half" ${qm === "half" ? "selected" : ""}>50%</option></select>` +
        `<span class="p-slx">Exit</span><i class="sld ${st[1]}" title="${st[0]}${r && r.msg ? " — " + r.msg.replace(/"/g, "'") : ""}"></i></td>`;
    }
    case "away": {
      if (!open || !p.away) return `<td class="pc-away"></td>`;
      return `<td class="pc-away"><span class="p-awl mono">—</span> <span class="dim">(${fp(p.away.strike, p.away.strike % 1 ? 1 : 0)})</span>` +
        inp(tok, "aw", 1) + `<button class="pb aw" data-x="away">Away</button><button class="pb" data-x="aw" data-fr="0.25">1/4</button><button class="pb" data-x="aw" data-fr="0.5">1/2</button></td>`;
    }
    case "close": {
      if (!open || !p.close) return `<td class="pc-close"></td>`;
      return `<td class="pc-close"><span class="p-cll mono">—</span> <span class="dim">(${fp(p.close.strike, p.close.strike % 1 ? 1 : 0)})</span>` +
        inp(tok, "cl", 1) + `<button class="pb cl" data-x="closer" title="Move toward the money by the strike gap">Close</button>` +
        `<button class="pb" data-x="cl" data-fr="0.25">1/4</button><button class="pb" data-x="cl" data-fr="0.5">1/2</button></td>`;
    }
    case "exit":
      if (!open) return `<td class="pc-exit"></td>`;
      return `<td class="pc-exit">${inp(tok, "ex", 1)}<button class="pb ex" data-x="exit">Exit</button><button class="pb" data-x="ex" data-fr="0.25">1/4</button>` +
        `<button class="pb" data-x="ex" data-fr="0.5">1/2</button><button class="pb" data-x="ex" data-fr="1">1</button></td>`;
    case "add":
      if (!open) return `<td class="pc-add"></td>`;
      return `<td class="pc-add">${inp(tok, "ad", 1)}<button class="pb ad" data-x="add">Add</button><button class="pb" data-x="ad" data-fr="0.25">1/4</button>` +
        `<button class="pb" data-x="ad" data-fr="0.5">1/2</button><button class="pb" data-x="ad" data-fr="1">1</button></td>`;
  }
  return "<td></td>";
}

function renderPositions() {
  const tb = $("pos-rows");
  if (tb.contains(document.activeElement) && /INPUT|SELECT/.test(document.activeElement.tagName)) { S.posDirty = true; return; }
  S.posDirty = false;
  renderPosHead();
  const cols = pCols();
  const all = S.book.positions;
  const rows = pShown();
  if (!all.length || !rows.length) {
    tb.innerHTML = `<tr><td colspan="${cols.length}" class="empty">${all.length ? "No position matches the filters." : "No positions yet."}</td></tr>`;
    paintPnl(); return;
  }
  const rowHtml = (p) => p.mid
    ? `<tr class="p-mid"><td colspan="${cols.length}"><span>${p.name} FUT <b class="p-midpx" data-n="${p.name}" data-e="${p.expiry}">${fp(pFutPx(p.name, p.expiry))}</b></span></td></tr>`
    : `<tr class="${p.net ? "" : "flat"}" data-tok="${p.token}">${cols.map((c) => pCell(p, c.k)).join("")}</tr>`;
  let html = "";
  if (S.pGroup) {
    const groups = [["sell", "SELL POSITIONS", (p) => p.net < 0], ["buy", "BUY POSITIONS", (p) => p.net > 0], ["closed", "CLOSED", (p) => !p.net]];
    for (const [id, title, test] of groups) {
      const g = rows.filter(test);
      if (!g.length) continue;
      const ce = g.filter((p) => p.kind === "CE").reduce((s, p) => s + pLots(p), 0);
      const pe = g.filter((p) => p.kind === "PE").reduce((s, p) => s + pLots(p), 0);
      const net = g.reduce((s, p) => s + pLots(p), 0);
      const coll = S.pColl.has(id);
      html += `<tr class="p-gh" data-g="${id}"><td colspan="${cols.length}" class="l">${coll ? "▸" : "▾"} <b>${title}</b>` +
        ` <span class="dim">COUNT</span> ${g.length}` + (id !== "closed" ? ` <span class="dim">QTY CE</span> ${ce} <span class="dim">PE</span> ${pe} <span class="dim">NET</span> ${net}` : "") + `</td></tr>`;
      if (!coll) html += ladder(g).map(rowHtml).join("");
    }
  } else html = ladder(rows).map(rowHtml).join("");
  html += `<tr class="tot">${cols.map((c) => c.k === "symbol" ? `<td class="l">Total</td>` : c.k === "pnl" ? `<td id="pl-tot-open"></td>` :
    c.k === "realized" ? `<td id="pl-tot-real"></td>` : `<td></td>`).join("")}</tr>`;
  tb.innerHTML = html;
  const sel = $("p-all");
  if (sel) sel.checked = rows.length > 0 && rows.every((p) => S.pSel.has(p.token));
  const sla = $("p-slall");
  if (sla) { const o = rows.filter((p) => p.net && (p.kind === "CE" || p.kind === "PE")); sla.checked = o.length > 0 && o.every((p) => { const r = (S.book.sl || {})[p.token]; return r && r.state !== "hit"; }); }
  paintPnl();
}
// re-render once the owner leaves a box (renders are held while typing)
$("pos-rows").addEventListener("focusout", () => setTimeout(() => { if (S.posDirty) renderPositions(); }, 0));
$("pos-rows").addEventListener("input", (e) => {
  const el = e.target.closest("[data-in]"), tr = e.target.closest("tr[data-tok]");
  if (el && tr) S.pin[tr.dataset.tok + ":" + el.dataset.in] = el.value;
});

const live = () => S.mode === "LIVE";
function lotsFor(p, fr_) { const n = Math.abs(pLots(p)); return Math.max(1, Math.floor(n * fr_ + 1e-9)); }
function typed(tr, f) { const el = tr.querySelector(`[data-in="${f}"]`); const v = Math.floor(+(el && el.value) || 0); return v > 0 ? v : 0; }
function doAway(p, lots, away = true) {
  const n = Math.abs(pLots(p));
  send({ op: "move", token: p.token, steps: +$("move-n").value, away, lots: lots >= n ? null : lots, req: ++S.req });
}
function doExit(p, lots) {
  const n = Math.abs(pLots(p));
  sendLegs([{ tok: p.token, side: p.net > 0 ? "S" : "B" }], Math.min(lots, n), null, "positions exit");
}
function doAdd(p, lots) { startLegs([{ tok: p.token, side: p.net > 0 ? "B" : "S" }], { lots, source: "add", noAsk: true }); }
function slSet(tok, extra) { send({ op: "sl_set", token: tok, ...extra, req: ++S.req }); }

$("pos-rows").addEventListener("click", (e) => {
  const gh = e.target.closest("tr.p-gh");
  if (gh) { const g = gh.dataset.g; S.pColl.has(g) ? S.pColl.delete(g) : S.pColl.add(g); LS.set("p.coll", [...S.pColl]); return renderPositions(); }
  const tr = e.target.closest("tr[data-tok]");
  if (!tr) return;
  const tok = +tr.dataset.tok;
  const p = S.book.positions.find((x) => x.token === tok);
  if (!p) return;
  if (e.target.classList.contains("p-sel")) { e.target.checked ? S.pSel.add(tok) : S.pSel.delete(tok); return; }
  if (e.target.classList.contains("p-slon")) {
    if (!e.target.checked && live() && !confirm(`Remove the stop-loss of ${p.symbol}? It is cancelled at Kotak and not re-armed until the position is closed (Apply or this tick puts it back).`)) { e.target.checked = true; return; }
    delete S.pin[tok + ":slpx"];
    return slSet(tok, { on: e.target.checked });
  }
  const b = e.target.closest("button[data-x]");
  if (!b) return;
  const x = b.dataset.x, f = +b.dataset.fr;
  if (x === "away") return doAway(p, typed(tr, "aw") || 1);
  if (x === "aw") return doAway(p, lotsFor(p, f));
  if (x === "closer") return doAway(p, typed(tr, "cl") || 1, false);
  if (x === "cl") return doAway(p, lotsFor(p, f), false);
  if (x === "exit") return doExit(p, typed(tr, "ex") || 1);
  if (x === "ex") return f >= 1 ? send({ op: "exit", token: tok, req: ++S.req }) : doExit(p, lotsFor(p, f));
  if (x === "add") return doAdd(p, typed(tr, "ad") || 1);
  if (x === "ad") return doAdd(p, lotsFor(p, f));
});
$("pos-rows").addEventListener("change", (e) => {
  const tr = e.target.closest("tr[data-tok]");
  if (!tr) return;
  const tok = +tr.dataset.tok;
  if (e.target.dataset.in === "slpx") {
    const v = +e.target.value;
    if (!(v > 0)) return;
    delete S.pin[tok + ":slpx"];
    return slSet(tok, { on: true, trigger: v });
  }
  if (e.target.classList.contains("p-slq")) return slSet(tok, { on: true, qty: e.target.value });
});
$("pos-rows").addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.dataset.in === "slpx") e.target.blur(); });

// header: select all, SL all, filters
$("pos-head").addEventListener("click", (e) => {
  if (e.target.id === "p-all") {
    const rows = pShown();
    rows.forEach((p) => (e.target.checked ? S.pSel.add(p.token) : S.pSel.delete(p.token)));
    return renderPositions();
  }
  if (e.target.id === "p-slall") {
    const rows = pShown().filter((p) => p.net && (p.kind === "CE" || p.kind === "PE"));
    if (!rows.length) return;
    const on = e.target.checked;
    if (live() && !confirm(on ? `Put a stop-loss on ${rows.length} position(s) shown - REAL orders at Kotak?` :
                                `Remove the stop-loss of ${rows.length} position(s) shown?`)) { e.target.checked = !on; return; }
    rows.forEach((p) => { const r = (S.book.sl || {})[p.token]; if (on !== !!(r && r.state !== "hit")) slSet(p.token, { on }); });
    return;
  }
  const fb = e.target.closest("button.pf");
  if (fb) { e.stopPropagation(); openFilter(fb); }
});
function openFilter(btn) {
  const k = btn.dataset.f, m = $("p-filt");
  const vals = [...new Set(S.book.positions.map((p) => pVal(p, k)))].sort((a, b) => (isFinite(a) && isFinite(b) ? a - b : a < b ? -1 : 1));
  const cur = S.pf[k];
  m.innerHTML = `<div class="pf-h">${PCOLS.find((c) => c.k === k).h}</div><input class="pf-q" placeholder="search…">` +
    `<div class="pf-l">${vals.map((v) => `<label><input type="checkbox" value="${v}" ${!cur || cur.has(v) ? "checked" : ""}> ${v || "—"}</label>`).join("")}</div>` +
    `<div class="pf-b"><button class="btn sm" data-a="all">All</button><button class="btn sm" data-a="none">None</button><button class="btn sm go" data-a="ok">OK</button></div>`;
  const r = btn.getBoundingClientRect();
  m.style.left = Math.min(r.left, innerWidth - 240) + "px";
  m.style.top = Math.max(8, r.top - 8 - Math.min(320, 90 + vals.length * 24)) + "px";
  m.hidden = false;
  m.dataset.k = k;
  m.querySelector(".pf-q").oninput = (ev) => m.querySelectorAll(".pf-l label").forEach((l) => (l.hidden = !l.textContent.toLowerCase().includes(ev.target.value.toLowerCase())));
}
$("p-filt").addEventListener("click", (e) => {
  e.stopPropagation();
  const a = e.target.dataset.a, m = $("p-filt");
  if (!a) return;
  const boxes = [...m.querySelectorAll(".pf-l input")];
  if (a === "all") return boxes.forEach((b) => (b.checked = true));
  if (a === "none") return boxes.forEach((b) => (b.checked = false));
  const on = boxes.filter((b) => b.checked).map((b) => b.value);
  if (on.length === boxes.length) delete S.pf[m.dataset.k]; else S.pf[m.dataset.k] = new Set(on);
  m.hidden = true;
  renderPositions();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#p-filt")) $("p-filt").hidden = true;
  if (!e.target.closest("#p-colmenu") && e.target.id !== "p-cols") $("p-colmenu").hidden = true;
});

// toolbar
$("p-group").classList.toggle("on", S.pGroup);
$("p-group").onclick = () => { S.pGroup = !S.pGroup; LS.set("p.group", S.pGroup); $("p-group").classList.toggle("on", S.pGroup); renderPositions(); };
$("p-cols").onclick = () => {
  const m = $("p-colmenu");
  const items = PGROUPS.map((g) => `<div class="pcm-g"><label><input type="checkbox" data-c="${g}" ${S.pHide.has(g) ? "" : "checked"}> <b>${g}</b></label>` +
    PCOLS.filter((c) => c.g === g && c.h && !c.h.startsWith("<")).map((c) =>
      `<label class="pcm-c"><input type="checkbox" data-c="${c.k}" ${S.pHide.has(c.k) ? "" : "checked"}> ${c.h}</label>`).join("") + `</div>`).join("");
  m.innerHTML = items;
  const r = $("p-cols").getBoundingClientRect();
  m.style.left = Math.max(8, r.right - 260) + "px";
  m.style.top = Math.max(8, r.top - 8 - 420) + "px";
  m.hidden = !m.hidden;
};
$("p-colmenu").addEventListener("change", (e) => {
  const c = e.target.dataset.c;
  if (!c) return;
  e.target.checked ? S.pHide.delete(c) : S.pHide.add(c);
  LS.set("p.hide", [...S.pHide]);
  renderPositions();
});
$("p-csv").onclick = () => {
  const cols = PCOLS.filter((c) => ["symbol", "name", "strike", "kind", "expiry", "product", "net", "avg", "realized", "bval", "sval"].includes(c.k));
  const lines = [[...cols.map((c) => c.h), "LTP", "PNL", "SL"].join(",")];
  for (const p of pShown()) {
    const r = posPnl(p), sl = (S.book.sl || {})[p.token];
    lines.push([...cols.map((c) => c.k === "avg" ? p.avg : c.k === "realized" ? p.realized : c.k === "bval" ? p.bval || 0 :
      c.k === "sval" ? p.sval || 0 : pVal(p, c.k)), r.ltp ?? "", r.total.toFixed(2), sl && sl.trigger != null ? sl.trigger : ""].join(","));
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  a.download = `positions_${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
};
function selRows() { return S.book.positions.filter((p) => p.net && S.pSel.has(p.token)); }
$("p-exit-sel").onclick = () => {
  const rows = selRows(), f = +$("p-size").value;
  if (!rows.length) return toast("tick the rows to exit first", true);
  if (live() && !confirm(`Exit ${f * 100}% of ${rows.length} selected position(s) on Kotak?`)) return;
  rows.forEach((p) => (f >= 1 ? send({ op: "exit", token: p.token, req: ++S.req }) : doExit(p, lotsFor(p, f))));
};
$("p-add-sel").onclick = () => {
  const rows = selRows(), f = +$("p-size").value;
  if (!rows.length) return toast("tick the rows to add to first", true);
  if (live() && !confirm(`Add ${f * 100}% to each of ${rows.length} selected position(s) on Kotak?`)) return;
  rows.forEach((p) => doAdd(p, lotsFor(p, f)));
};
$("p-cancel").onclick = () => send({ op: "cancel_all", req: ++S.req });


// ── per-position auto stop-loss (engine: sl.py) ──
const SL_KEYS = ["arm_on", "sl_auto", "sl_val", "sl_unit", "qty", "on_hit", "trail_auto", "trail_on", "trail_every"];
function slCfg() { const v = S.settings || (S.meta && S.meta.settings) || {}; return v.auto_sl || {}; }
function paintSlBar() {
  const c = slCfg();
  for (const k of SL_KEYS) {
    const el = $("sl-" + k);
    if (!el || c[k] == null || document.activeElement === el) continue;
    if (el.type === "checkbox") el.checked = !!c[k]; else el.value = c[k];
  }
  const live = S.mode === "LIVE";
  const w = $("sl-where");
  w.textContent = live ? "● At broker" : "● Watched (paper)";
  w.className = "slbadge " + (live ? "on" : "");
}
function saveSlBar() {
  const v = {};
  for (const k of SL_KEYS) {
    const el = $("sl-" + k);
    v[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value;
  }
  if (!(v.sl_val > 0) || !(v.trail_every > 0)) { paintSlBar(); return toast("stop-loss and trail values must be above 0", true); }
  if (v.sl_auto && !slCfg().sl_auto && S.mode === "LIVE" &&
      !confirm(`Auto stop-loss ON (LIVE): every ${v.arm_on === "all" ? "" : v.arm_on + " "}option position on Kotak P gets a REAL stop-loss order at ${v.sl_val}${v.sl_unit === "%" ? "%" : " pts"} from its entry, including positions already open. Continue?`)) {
    $("sl-sl_auto").checked = false; return;
  }
  send({ op: "settings_set", values: { auto_sl: v }, tag: "slbar", req: ++S.req });
}
SL_KEYS.forEach((k) => { const el = $("sl-" + k); if (el) el.addEventListener("change", saveSlBar); });
$("sl-apply").onclick = () => {
  const v = { val: +$("sl-sl_val").value, unit: $("sl-sl_unit").value, on: $("sl-arm_on").value };
  const n = (S.book ? S.book.positions : []).filter((p) => p.net && /(CE|PE)$/.test(p.symbol) &&
    (v.on === "all" || (v.on === "sell" ? p.net < 0 : p.net > 0))).length;
  if (!n) return toast(`no open ${v.on === "all" ? "" : v.on + " "}option position`, true);
  if (S.mode === "LIVE" && !confirm(`Place / move the stop-loss of ${n} open ${v.on === "all" ? "" : v.on + " "}position(s) to ${v.val}${v.unit === "%" ? "%" : " pts"} from entry - REAL orders at Kotak. Continue?`)) return;
  const values = {};
  for (const k of SL_KEYS) { const el = $("sl-" + k); values[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? +el.value : el.value; }
  if (!(values.sl_val > 0) || !(values.trail_every > 0)) return toast("stop-loss and trail values must be above 0", true);
  send({ op: "sl_apply", values, req: ++S.req });
};
function paintPnl() {
  let total = 0, open = 0, real = 0;
  const per = {};                                        // instrument MTM for the right panel
  for (const p of S.book.positions) {
    const r = posPnl(p);
    total += r.total; open += r.open; real += p.realized;
    per[p.name] = (per[p.name] || 0) + r.total;
    const tr = document.querySelector(`#pos-rows tr[data-tok="${p.token}"]`);
    if (tr) {
      const l = tr.querySelector(".pl-ltp");
      if (l) l.textContent = fp(r.ltp);
      const o = tr.querySelector(".pl-open");
      if (o) {
        o.textContent = fr(r.total);
        o.className = "pl-open " + (r.total > 0 ? "up-t" : r.total < 0 ? "dn-t" : "");
      }
      const aw = p.away && tr.querySelector(".p-awl");
      if (aw) { const t = S.T.get(p.away.token); aw.textContent = t ? fp(t[LTP]) : "—"; }
      const mn = tr.querySelector(".p-mny");
      if (mn) { const m = money(p); if (mn.textContent !== m) { mn.textContent = m; mn.className = "p-mny " + (m === "ATM" ? "atm" : m.startsWith("ITM") ? "itm" : ""); } }
      const cl = p.close && tr.querySelector(".p-cll");
      if (cl) { const t = S.T.get(p.close.token); cl.textContent = t ? fp(t[LTP]) : "—"; }
    }
  }
  const to = $("pl-tot-open"), tr_ = $("pl-tot-real");
  if (to) { to.textContent = fr(total); to.className = total > 0 ? "up-t" : total < 0 ? "dn-t" : ""; }
  document.querySelectorAll(".p-midpx").forEach((b) => (b.textContent = fp(pFutPx(b.dataset.n, b.dataset.e))));
  const pm = $("p-mtm");
  if (pm) { pm.textContent = "MTM " + fr(total); pm.className = "pmtm mono " + (total > 0 ? "up-t" : total < 0 ? "dn-t" : ""); }
  if (tr_) { tr_.textContent = fr(real); tr_.className = real > 0 ? "up-t" : real < 0 ? "dn-t" : ""; }
  const el = $("pnl");
  el.textContent = fr(total);
  el.className = "val mono " + (total > 0 ? "up-t" : total < 0 ? "dn-t" : "");
  const big = $("cp-pnl");
  big.textContent = fr(total);
  big.className = "cp-big mono " + (total > 0 ? "up-t" : total < 0 ? "dn-t" : "");
  const iv = per[$("cp-inst").value] || 0;
  $("cp-imtm").textContent = fr(iv);
  $("cp-imtm").className = "cp-imtm mono " + (iv > 0 ? "up-t" : iv < 0 ? "dn-t" : "");
}

function renderOrders() {
  let rows = S.book.orders;
  if (S.ofilter === "open") rows = rows.filter(working);
  else if (S.ofilter === "done") rows = rows.filter((o) => !working(o));
  const tb = $("ord-rows");
  if (!rows.length) { tb.innerHTML = `<tr><td colspan="10" class="empty">No orders.</td></tr>`; return; }
  tb.innerHTML = rows.map((o) => `<tr>
    <td class="l">${o.ts}</td><td class="l">${o.symbol}${o.slice ? ` <span class="dim">[${o.slice}]</span>` : ""}</td>
    <td class="side-${o.side}">${o.side === "B" ? "BUY" : "SELL"}</td>
    <td>${o.qty / o.lot} lot${o.qty / o.lot === 1 ? "" : "s"}</td><td>${o.filled / o.lot}</td>
    <td>${fp(o.limit)}${o.kind === "MKT" ? " <span class='dim'>mkt</span>" : ""}</td><td>${o.filled ? fp(o.avg) : "—"}</td>
    <td class="l st-${o.status}">${o.status}</td><td class="detail" title="${(o.reason || "").replace(/"/g, "&quot;")}">${o.reason || ""}</td>
    <td>${o.status === "OPEN" ? `<button class="btn sm" data-c="${o.id}">Cancel</button>` : ""}</td></tr>`).join("");
}
$("ord-rows").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-c]");
  if (b) send({ op: "cancel", id: +b.dataset.c, req: ++S.req });
});
document.querySelectorAll("#ofilter button").forEach((b) => (b.onclick = () => {
  S.ofilter = b.dataset.f;
  document.querySelectorAll("#ofilter button").forEach((x) => x.classList.toggle("on", x === b));
  renderOrders();
}));
document.querySelectorAll(".dt").forEach((b) => (b.onclick = () => {
  const p = b.dataset.p;
  document.querySelectorAll(".dt").forEach((x) => x.classList.toggle("on", x === b));
  $("pos-table").hidden = p !== "pos"; $("tools-pos").hidden = p !== "pos";
  $("ord-table").hidden = p !== "ord"; $("tools-ord").hidden = p !== "ord";
}));

// green / red edge on chain prices where I hold a position
function markPositions() {
  document.querySelectorAll("#rows td.pos-long, #rows td.pos-short").forEach((td) => td.classList.remove("pos-long", "pos-short"));
  const lots = new Map();
  for (const p of S.book.positions) if (p.net) lots.set(p.token, p.net / p.lot);
  for (const [tok, c] of S.cells) {
    const l = lots.get(tok);
    set(c.pos, l ? (l > 0 ? "+" : "") + l : "");
    c.pos.className = "pos" + (l > 0 ? " long" : l < 0 ? " short" : "");
    if (l) c.ltp.classList.add(l > 0 ? "pos-long" : "pos-short");
  }
}

// ── status / footer / clock ─────────────────────────────────────────────────
function onStatus(m) {
  const f = m.feed || {};
  S.mode = m.mode || "PAPER";
  if ($("sl-where")) paintSlBar();
  const live = S.mode === "LIVE";
  const extra = !live && m.live_open ? ` · ${m.live_open} LIVE OPEN` : "";
  $("mode").textContent = live ? "● LIVE · KOTAK P" : "PAPER" + extra;
  $("mode").className = "badge " + (live ? "live" : "paper") + (extra ? " warn" : "");
  $("mode").title = live ? `LIVE armed by ${(m.arm || {}).by || "?"} at ${(m.arm || {}).at || "?"} · click to go back to PAPER`
    : "PAPER orders · click to arm LIVE Kotak P orders (owner only)";
  document.body.classList.toggle("live-mode", live);
  const ps = m.pos_sync || {};
  const src = $("pos-src");
  if (src) {
    src.textContent = !live ? "" : ps.ok ? "mirrored from Kotak ✓" : "⚠ Kotak positions NOT readable - may be stale";
    src.className = "possrc mono" + (live && !ps.ok ? " bad" : "");
  }
  if (live && ps.ok === false && ps.age !== null) toastOnce("possync", "Kotak positions NOT readable - what you see may be stale. " + (ps.msg || ""), true);
  const b = m.broker || {};
  if (live && b.ok === false) toastOnce("broker", "Kotak order book not readable: " + (b.msg || ""), true);
  if (!f.authed) setFeed(f.connected ? "authenticating…" : "feed down", "bad");
  $("f-sub").textContent = `subscribed ${f.subscribed ?? 0} · depth ${f.depth ?? 0} · reconnects ${Math.max(0, (f.connects || 1) - 1)}`;
  $("f-msg").textContent = m.error || f.error || "";
}

setInterval(() => {
  const rate = S.recv - S.recvShown;
  S.recvShown = S.recv;
  const age = S.lastTickAt ? (Date.now() - S.lastTickAt) / 1000 : null;
  if (S.ws && S.ws.readyState === 1) {
    if (age != null && age < 10) setFeed(`LIVE · ${rate} t/s`, "ok");
    else setFeed(age == null ? "waiting for ticks" : "no ticks (market closed?)", "");
  }
  $("f-age").textContent = age == null ? "no ticks yet" : `last tick ${age.toFixed(1)} s ago`;
  $("clock").textContent = new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour12: false });
}, 1000);


// ── Stage 3: views (chain / straddle / strangle), hedges, risk ─────────────
S.view = LS.get("view", "chain");
S.hedge = LS.get("hedge", false);
S.hedgeN = LS.get("hedgeN", 2);
S.combo = [];                    // [{a, b, cells}] rows of the straddle / strangle table
S.risk = {};

$("hedge-d").innerHTML = Array.from({ length: 10 }, (_, i) => `<option value="${i + 1}">${i + 1} out</option>`).join("");
$("hedge-d").value = S.hedgeN;
$("hedge-d").onchange = (e) => { S.hedgeN = +e.target.value; LS.set("hedgeN", S.hedgeN); };
function paintHedge() { $("tg-hedge").classList.toggle("on", S.hedge); $("hedge-d").disabled = !S.hedge; }
$("tg-hedge").onclick = () => { S.hedge = !S.hedge; LS.set("hedge", S.hedge); paintHedge(); };
paintHedge();

function setView(v) {
  S.view = v; LS.set("view", v);
  document.querySelectorAll("#view button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("chain").hidden = v !== "chain";
  $("combo").hidden = v === "chain";
  if (v !== "chain") buildCombo();
}
document.querySelectorAll("#view button").forEach((b) => (b.onclick = () => setView(b.dataset.v)));

// straddle: CE + PE of the same strike · strangle: CE i strikes above ATM + PE i strikes below
function buildCombo() {
  const m = S.chain;
  if (!m || S.view === "chain") return;
  const rows = gridRows();                   // already on the strike-gap grid
  const ai = rows.findIndex((r) => r.k === S.atm);
  if (ai < 0) return;
  const n = S.cur.strikes;
  const pairs = [];
  if (S.view === "straddle") {
    for (const r of S.visRows) pairs.push({ a: r.ce, b: r.pe, ka: r.k, kb: r.k });
    $("cb-h1").textContent = "Strike"; $("cb-h2").textContent = "";
  } else {
    for (let i = 0; i <= n; i++) {
      const up = rows[ai + i], dn = rows[ai - i];
      if (up && dn) pairs.push({ a: up.ce, b: dn.pe, ka: up.k, kb: dn.k, w: i });
    }
    $("cb-h1").textContent = "Call strike"; $("cb-h2").textContent = "Put strike";
  }
  const tb = $("crows");
  tb.innerHTML = "";
  S.combo = pairs.map((p) => {
    const tr = document.createElement("tr");
    const isStr = S.view === "straddle";
    tr.innerHTML = `<td class="k">${p.ka}${isStr && p.ka === S.atm ? " ATM" : ""}</td><td class="ltp"></td>` +
      `<td class="${isStr ? "" : "k2"}">${isStr ? "" : p.kb}</td><td class="ltp"></td><td class="sum"></td><td class="c"></td>` +
      `<td class="act2"><button class="btn sm sell2" data-a="S">Sell both</button><button class="btn sm buy2" data-a="B">Buy both</button></td>`;
    if (isStr && p.ka === S.atm) tr.classList.add("atm");
    tr.dataset.a = p.a; tr.dataset.b = p.b;
    tb.appendChild(tr);
    const td = tr.children;
    return { ...p, ca: td[1], cb: td[3], sum: td[4], chg: td[5] };
  });
  paintCombo(null);
}

function paintCombo(list) {
  const touched = list ? new Set(list.map((t) => t[TOK])) : null;
  for (const r of S.combo) {
    if (touched && !touched.has(r.a) && !touched.has(r.b)) continue;
    const ta = S.T.get(r.a), tb = S.T.get(r.b);
    const la = ta ? ta[LTP] : null, lb = tb ? tb[LTP] : null;
    set(r.ca, fp(la)); set(r.cb, fp(lb));
    const sum = la != null && lb != null ? la + lb : null;
    const prev = r.prev;
    set(r.sum, fp(sum));
    const pcs = ta && tb && ta[PC] && tb[PC] ? ta[PC] + tb[PC] : null;
    const ch = pct(sum, pcs);
    set(r.chg, ch == null ? "—" : (ch > 0 ? "+" : "") + ch.toFixed(2));
    r.chg.className = "c " + (ch > 0 ? "up" : ch < 0 ? "dn" : "");
    if (prev != null && sum != null && sum !== prev) {
      r.k = (r.k || 0) ^ 1;
      r.sum.classList.remove("fu1", "fu2", "fd1", "fd2");
      r.sum.classList.add((sum > prev ? "fu" : "fd") + (r.k ? "1" : "2"));
    }
    r.prev = sum;
  }
}

$("crows").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-a]");
  if (!b) return;
  const tr = b.closest("tr");
  startLegs([{ tok: +tr.dataset.a, side: b.dataset.a }, { tok: +tr.dataset.b, side: b.dataset.a }], { source: S.view });
});

// ── risk panel ──
function onRisk(m) {
  S.risk = m;
  const el = $("rk-state");
  const f = (v) => (v == null ? "—" : fr(v));
  let h;
  if (m.armed) {
    h = `<span class="armed">● ARMED</span> since ${m.set_at} · stop ${f(m.stop)} · target ${f(m.target)}` +
        (m.trail ? ` · trail ${nf.format(m.trail)}` : "") + ` · best P&L ${f(m.best)}`;
  } else if (m.fired) {
    h = `<span class="fired">● FIRED ${m.fired.at}</span> — ${m.fired.reason}. Exit orders sent. Press Set to arm again.`;
  } else h = "Guard off. Type a stop-loss and/or target (and an optional trail), then Set.";
  el.innerHTML = h;
  $("rk-set").classList.toggle("armed", !!m.armed);
  paintRiskSum();
}
$("btn-refresh").onclick = () => {
  $("btn-refresh").classList.add("spin");
  setTimeout(() => $("btn-refresh").classList.remove("spin"), 4000);
  if (!send({ op: "refresh", req: ++S.req })) { toast("not connected - reconnecting", true); return; }
  requestChain();                                         // fresh chain snapshot
  askInline();                                            // MTM + imbalance graphs
  if (!$("graph").hidden && $("ga-name")) askAtmHist();   // ATM chart if open
};
$("rk-set").onclick = () => send({ op: "risk_set", stop: $("rk-stop").value, target: $("rk-target").value,
                                   trail: $("rk-trail").value, req: ++S.req });
$("rk-clear").onclick = () => send({ op: "risk_clear", req: ++S.req });

// short-premium summary: what is collected on short CE vs short PE right now
function paintRiskSum() {
  let ce = 0, pe = 0, nce = 0, npe = 0, legs = 0;
  for (const p of S.book.positions) {
    if (!p.net) continue;
    legs++;
    const t = S.T.get(p.token);
    const ltp = t ? t[LTP] : p.avg;
    if (p.net < 0 && /CE$/.test(p.symbol)) { ce += ltp * -p.net * p.mult; nce += -p.net / p.lot; }
    if (p.net < 0 && /PE$/.test(p.symbol)) { pe += ltp * -p.net * p.mult; npe += -p.net / p.lot; }
  }
  const tot = ce + pe;
  const imb = tot ? ((ce - pe) / tot) * 100 : null;
  const imbCls = imb == null ? "" : Math.abs(imb) >= 20 ? " warn" : " ok";
  $("rk-sum").innerHTML =
    `<span class="sb${imbCls}" title="(short CE − short PE) ÷ total; above 0 = calls heavier">CE/PE imbalance <b>${imb == null ? "—" : (imb > 0 ? "+" : "") + imb.toFixed(1) + "%"}</b></span>` +
    `<span class="sb">Short CE premium <b>${nf2.format(ce)}</b> <i>(${nce} lots)</i></span>` +
    `<span class="sb">Short PE premium <b>${nf2.format(pe)}</b> <i>(${npe} lots)</i></span>` +
    `<span class="sb">Total <b>${nf2.format(tot)}</b></span>` +
    `<span class="sb">Open legs <b>${legs}</b></span>`;
}

setView(S.view);


// ── Stage 3b: settings, payoff graph, MTM graph ────────────────────────────
S.settings = null;
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => closeModal(b.dataset.close)));
["settings", "graph", "replays"].forEach((id) => $(id).addEventListener("click", (e) => { if (e.target.id === id) closeModal(id); }));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("settings").hidden) closeModal("settings");
  if (!$("graph").hidden) closeModal("graph");
  if (!$("replays").hidden) closeModal("replays");
});
function closeModal(id) {
  $(id).hidden = true;
  if (id === "graph") { clearInterval(S.gTimer); S.gTimer = null; }
}

const SKEYS = ["entry_ref", "entry_buffer", "entry_unit", "exit_ref", "exit_buffer", "exit_unit", "product", "session", "market_after_s"];
$("gear").onclick = () => {
  const v = S.settings || (S.meta && S.meta.settings);
  if (!v) return toast("settings not loaded yet", true);
  for (const k of SKEYS) $("s-" + k).value = v[k];
  $("s-reprice").checked = !!v.reprice;
  const gc = S.meta.gap_choices || {};
  $("s-gaps").innerHTML = Object.values(S.prods).filter((p) => gc[p.name]).map((p) =>
    `<label>${p.label}<select data-gap="${p.name}">` +
    gc[p.name].map((g) => `<option value="${g}">${nf.format(g)}</option>`).join("") + `</select></label>`).join("");
  document.querySelectorAll("#s-gaps select").forEach((s) => (s.value = (v.strike_gap || {})[s.dataset.gap] || gc[s.dataset.gap][0]));
  $("s-note").textContent = "";
  $("settings").hidden = false;
};
$("s-save").onclick = () => {
  const values = {};
  for (const k of SKEYS) values[k] = $("s-" + k).value;
  values.reprice = $("s-reprice").checked;
  values.strike_gap = {};
  document.querySelectorAll("#s-gaps select").forEach((s) => (values.strike_gap[s.dataset.gap] = +s.value));
  send({ op: "settings_set", values, req: ++S.req });
  $("s-note").textContent = "saving…";
};
function onSettingsAck(m) {
  if (m.settings) S.settings = m.settings;
  $("s-note").textContent = m.ok ? "saved ✓" : m.msg;
  if (m.ok) { setTimeout(() => closeModal("settings"), 500); S.atm = null; layout(); }
}
// the owner's strike gap for the current instrument, in points (0 = every listed strike)
function gapPts() {
  const v = S.settings || (S.meta && S.meta.settings) || {};
  return +((v.strike_gap || {})[S.cur.name]) || 0;
}
// chain rows on the strike-gap grid (strike a whole multiple of the gap); all rows if the grid is too thin
function gridRows() {
  const m = S.chain, g = gapPts();
  if (!m) return [];
  if (!g) return m.rows;
  const f = m.rows.filter((r) => Math.abs(r.k / g - Math.round(r.k / g)) < 1e-9);
  return f.length >= 3 ? f : m.rows;
}

// ── small SVG chart helper (one series, zero line, x labels) ──
function svgChart(xs, ys, o = {}) {
  // o.W / o.H: draw at the box's real pixel size (small side charts) so lines and text are not shrunk
  const W = Math.round(o.W || 820), H = Math.round(o.H || 300), sm = W < 600;
  const L = sm ? 52 : 64, R = o.now ? (sm ? 72 : 84) : 16, T = sm ? 8 : 14, B = sm ? 22 : 30;
  if (sm) o = { ...o, xticks: Math.max(1, Math.min(o.xticks || 6, Math.floor((W - L - R) / 70))) };
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  // o.level: a price-level line on its own scale (e.g. ATM straddle), not P&L around zero
  let y0 = o.level ? Math.min(...ys) : Math.min(0, ...ys), y1 = o.level ? Math.max(...ys) : Math.max(0, ...ys);
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.08; y0 -= pad; y1 += pad;
  const X = (v) => L + ((v - x0) / (x1 - x0 || 1)) * (W - L - R);
  const Y = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);
  const pts = xs.map((x, i) => `${X(x).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" ");
  const zy = Y(0).toFixed(1);
  const id = "c" + Math.random().toString(36).slice(2, 7);
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + ((y1 - y0) * i) / 4, y = Y(v).toFixed(1);
    grid += `<line class="g-grid" x1="${L}" x2="${W - R}" y1="${y}" y2="${y}"/><text class="g-label" x="${L - 6}" y="${(+y + 3.5).toFixed(1)}" text-anchor="end">${nf.format(Math.round(v))}</text>`;
  }
  let xl = "";
  const n = o.xticks || 6;
  for (let i = 0; i <= n; i++) {
    const v = x0 + ((x1 - x0) * i) / n;
    xl += `<text class="g-label" x="${X(v).toFixed(1)}" y="${H - 10}" text-anchor="middle">${(o.xfmt || ((t) => nf.format(Math.round(t))))(v)}</text>`;
  }
  const area = `${X(xs[0]).toFixed(1)},${zy} ${pts} ${X(xs[xs.length - 1]).toFixed(1)},${zy}`;
  const mark = o.mark != null && o.mark >= x0 && o.mark <= x1
    ? `<line class="g-spot" x1="${X(o.mark).toFixed(1)}" x2="${X(o.mark).toFixed(1)}" y1="${T}" y2="${H - B}"/>` +
      `<text class="g-spot-l" x="${(X(o.mark) + 5).toFixed(1)}" y="${T + 12}">${o.markLabel || ""}</text>` : "";
  svgChart.last = { xs, ys, X, Y, W, H, L, R, T, B, x0, x1, tip: o.tip };   // read by setChart() for hover
  let now = "";
  if (o.now) {                                            // current value: dashed line + price tag on the right
    const v = ys[ys.length - 1], y = Y(v);
    const t = o.nowFmt ? o.nowFmt(v) : nf2.format(v);
    now = `<line class="g-now" x1="${L}" x2="${W - R}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>` +
      `<circle class="g-now-tag" cx="${X(xs[xs.length - 1]).toFixed(1)}" cy="${y.toFixed(1)}" r="3.5"/>` +
      `<rect class="g-now-tag" x="${W - R + 3}" y="${(y - 9).toFixed(1)}" width="${R - 5}" height="18" rx="3"/>` +
      `<text class="g-now-txt" x="${W - R + 3 + (R - 5) / 2}" y="${(y + 4).toFixed(1)}" text-anchor="middle">${t}</text>`;
  }
  if (o.level) {
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${o.label || "chart"}">${grid}
      <polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2"/>
      <line class="g-axis" x1="${L}" x2="${L}" y1="${T}" y2="${H - B}"/>${mark}${now}${xl}<g class="g-hov" visibility="hidden"><line class="g-hl"/><circle class="g-hd" r="4"/><rect class="g-hb" rx="4"/><text class="g-ht"></text></g></svg>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${o.label || "chart"}">
    <defs><clipPath id="${id}a"><rect x="0" y="0" width="${W}" height="${zy}"/></clipPath>
          <clipPath id="${id}b"><rect x="0" y="${zy}" width="${W}" height="${H}"/></clipPath></defs>
    ${grid}
    <polygon points="${area}" fill="var(--up)" opacity=".16" clip-path="url(#${id}a)"/>
    <polygon points="${area}" fill="var(--down)" opacity=".16" clip-path="url(#${id}b)"/>
    <polyline points="${pts}" fill="none" stroke="var(--up)" stroke-width="2" clip-path="url(#${id}a)"/>
    <polyline points="${pts}" fill="none" stroke="var(--down)" stroke-width="2" clip-path="url(#${id}b)"/>
    <line class="g-zero" x1="${L}" x2="${W - R}" y1="${zy}" y2="${zy}"/>
    <line class="g-axis" x1="${L}" x2="${L}" y1="${T}" y2="${H - B}"/>
    ${mark}${now}${xl}<g class="g-hov" visibility="hidden"><line class="g-hl"/><circle class="g-hd" r="4"/><rect class="g-hb" rx="4"/><text class="g-ht"></text></g></svg>`;
}

// ── chart hover: crosshair + dot + value box; survives the live redraws (setChart re-applies it) ──
const CH = new WeakMap();                                 // chart box -> data of the chart drawn in it
S.hov = null;                                             // {box, cx} while the mouse is over a chart
function setChart(box, html) {
  box.innerHTML = html;
  CH.set(box, svgChart.last);
  if (S.hov && S.hov.box === box) hoverAt(box, S.hov.cx);
}
const hms = (t) => new Date(t * 1000).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata" });
function hoverAt(box, cx) {
  const d = CH.get(box), svg = box.querySelector("svg"), g = svg && svg.querySelector(".g-hov");
  if (!d || !g || !d.xs.length) return;
  const pt = svg.createSVGPoint();
  pt.x = cx; pt.y = 0;
  const sx = pt.matrixTransform(svg.getScreenCTM().inverse()).x;
  const xv = d.x0 + ((sx - d.L) / (d.W - d.L - d.R)) * (d.x1 - d.x0);
  let lo = 0, hi = d.xs.length - 1;                       // nearest point (xs ascending)
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (d.xs[m] < xv) lo = m; else hi = m; }
  const i = Math.abs(d.xs[lo] - xv) <= Math.abs(d.xs[hi] - xv) ? lo : hi;
  const x = d.X(d.xs[i]), y = d.Y(d.ys[i]);
  const lines = d.tip ? d.tip(i) : [hms(d.xs[i]), nf2.format(d.ys[i])];
  const hl = g.querySelector(".g-hl"), hd = g.querySelector(".g-hd"), hb = g.querySelector(".g-hb"), ht = g.querySelector(".g-ht");
  hl.setAttribute("x1", x); hl.setAttribute("x2", x); hl.setAttribute("y1", d.T); hl.setAttribute("y2", d.H - d.B);
  hd.setAttribute("cx", x); hd.setAttribute("cy", y);
  const w = Math.max(...lines.map((l) => l.length)) * 7.6 + 18, h = lines.length * 17 + 10;
  const bx = x + 12 + w > d.W - 4 ? x - 12 - w : x + 12;
  const by = Math.min(Math.max(d.T, y - h / 2), d.H - d.B - h);
  hb.setAttribute("x", bx); hb.setAttribute("y", by); hb.setAttribute("width", w); hb.setAttribute("height", h);
  ht.innerHTML = lines.map((l, k) => `<tspan x="${bx + 9}" y="${by + 18 + k * 17}"${k ? "" : ' class="g-ht0"'}></tspan>`).join("");
  ht.querySelectorAll("tspan").forEach((ts, k) => (ts.textContent = lines[k]));
  g.setAttribute("visibility", "visible");
}
document.addEventListener("mousemove", (e) => {
  const svg = e.target.closest && e.target.closest("svg");
  const box = svg && svg.parentElement;
  if (box && CH.has(box)) { S.hov = { box, cx: e.clientX }; hoverAt(box, e.clientX); return; }
  if (S.hov) {
    const g = S.hov.box.querySelector(".g-hov");
    if (g) g.setAttribute("visibility", "hidden");
    S.hov = null;
  }
});

// ── payoff at expiry of the open positions in the chain on screen ──
$("btn-payoff").onclick = () => {
  const m = S.chain;
  const legs = S.book.positions.filter((p) => p.net).map((p) => ({ p, i: S.sym.get(p.token) }))
    .filter((x) => x.i && x.i.name === m.name && x.i.ex === m.expiry);
  if (!m || !legs.length) return toast("open the instrument + expiry that holds your positions, then try again", true);
  const spot = futPrice();
  const ks = legs.map((x) => x.i.k);
  const lo = Math.min(spot || ks[0], ...ks) * 0.95, hi = Math.max(spot || ks[0], ...ks) * 1.05;
  const xs = [], ys = [];
  for (let j = 0; j <= 300; j++) {
    const S_ = lo + ((hi - lo) * j) / 300;
    xs.push(S_);
    ys.push(legs.reduce((a, { p, i }) => a + p.net * p.mult *
      ((i.side === "ce" ? Math.max(S_ - i.k, 0) : Math.max(i.k - S_, 0)) - p.avg), 0));
  }
  const be = [];
  for (let j = 1; j < ys.length; j++) if ((ys[j - 1] < 0) !== (ys[j] < 0)) be.push(xs[j - 1] + (xs[j] - xs[j - 1]) * (-ys[j - 1] / (ys[j] - ys[j - 1])));
  const now = legs.reduce((a, { p }) => a + posPnl(p).open, 0);
  $("g-title").textContent = `Payoff at expiry · ${m.fut ? m.fut.symbol.replace(/FUT$/, "") : m.name} options ${fmtDate(m.expiry)}`;
  $("g-tools").innerHTML = "";
  setChart($("g-body"), svgChart(xs, ys, { mark: spot, markLabel: `now ${fp(spot, 0)}`, label: "payoff at expiry",
                                       tip: (i) => [`At expiry ${nf.format(Math.round(xs[i]))}`, `P&L ${fr(ys[i])}`] }));
  $("g-note").innerHTML = `Max profit in range <b class="up-t">${fr(Math.max(...ys))}</b> · Max loss in range <b class="dn-t">${fr(Math.min(...ys))}</b>` +
    ` · Breakeven ${be.length ? be.map((b) => `<b>${nf.format(Math.round(b))}</b>`).join(" / ") : "<b>none in range</b>"}` +
    ` · Open P&L now <b>${fr(now)}</b><br><span class="dim">${legs.length} open leg(s) at their average prices, ±5 % around the strikes. ` +
    `Closed trades and time value are not included; the loss can be larger outside this range.</span>`;
  $("graph").hidden = false;
};

// ── MTM graph: today's Session P&L, one point per N minutes ──
S.g = { mins: LS.get("mtmMins", 1), inst: "all" };
if ($("btn-mtm")) $("btn-mtm").onclick = () => {
  $("g-title").textContent = "MTM graph · Session P&L today";
  $("g-tools").innerHTML = `One point per <select id="g-mins">${[1, 2, 5, 10, 15, 30, 60].map((n) => `<option value="${n}">${n} min</option>`).join("")}</select>` +
    ` <select id="g-inst"><option value="all">All instruments</option></select>`;
  $("g-mins").value = S.g.mins;
  $("g-mins").onchange = (e) => { S.g.mins = +e.target.value; LS.set("mtmMins", S.g.mins); askMtm(); };
  $("g-inst").onchange = (e) => { S.g.inst = e.target.value; askMtm(); };
  $("g-body").innerHTML = `<p class="g-note">Loading…</p>`;
  $("g-note").textContent = "";
  $("graph").hidden = false;
  askMtm();
  clearInterval(S.gTimer);
  S.gTimer = setInterval(askMtm, 5000);
};
function askMtm() { send({ op: "mtm_hist", mins: S.g.mins, inst: S.g.inst, req: ++S.req }); }
function onMtm(m) {
  if ($("graph").hidden || !$("g-inst")) return;
  const sel = $("g-inst");
  const sig = m.instruments.join();
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.innerHTML = `<option value="all">All instruments</option>` + m.instruments.map((n) => `<option value="${n}">${(S.prods[n] || {}).label || n}</option>`).join("");
    sel.value = S.g.inst;
  }
  const pts = m.points;
  if (pts.length < 2) {
    $("g-body").innerHTML = `<p class="g-note">Not enough points yet — the engine samples Session P&L every 5 s once the day has a trade.</p>`;
    $("g-note").textContent = "";
    return;
  }
  const hm = (t) => new Date(t * 1000).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" });
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const span = (xs[xs.length - 1] - xs[0]) / 60;
  setChart($("g-body"), svgChart(xs, ys, { xfmt: hm, label: "session P&L over time", xticks: Math.max(1, Math.min(6, Math.floor(span))),
                                       tip: (i) => [hms(xs[i]), `P&L ${fr(ys[i])}`] }));
  const mx = Math.max(...ys), mn = Math.min(...ys), ih = ys.indexOf(mx), il = ys.indexOf(mn);
  $("g-note").innerHTML = `Now <b class="${ys[ys.length - 1] >= 0 ? "up-t" : "dn-t"}">${fr(ys[ys.length - 1])}</b> · High <b class="up-t">${fr(mx)}</b> at ${hm(xs[ih])}` +
    ` · Low <b class="dn-t">${fr(mn)}</b> at ${hm(xs[il])} · ${pts.length} points, one per ${m.mins} min`;
}


// ── Replays: list recorded days, download the double-click file ─────────────
$("btn-replays").onclick = async () => {
  $("replays").hidden = false;
  const tb = $("rp-list").querySelector("tbody");
  tb.innerHTML = `<tr><td class="empty">Loading…</td></tr>`;
  try {
    const days = await (await fetch("/replays", { credentials: "include" })).json();
    if (!days.length) { tb.innerHTML = `<tr><td class="empty">No recorded days yet — a day is recorded from your first order.</td></tr>`; return; }
    tb.innerHTML = days.map((d) => `<tr>
      <td class="l">${fmtDate(d.day)}${d.today ? ` <span class="dim">(today, still recording)</span>` : ""}</td>
      <td class="dim">${d.html_mb != null ? d.html_mb + " MB" : "builds on download"}</td>
      <td><a class="btn sm go" href="/replays/MCX_${d.day}.html" download="MCX_${d.day}.html">Download</a></td></tr>`).join("");
  } catch (e) { tb.innerHTML = `<tr><td class="empty">Could not load the list: ${e}</td></tr>`; }
};


// ── right panel: margin (Kotak P account, read-only, cached by the engine) ──
function askMargin() { send({ op: "margin", req: ++S.req }); }
function onMargin(m) {
  const f = (v) => (v == null ? "—" : "₹ " + nf2.format(v));
  $("cp-mavail").textContent = f(m.available);
  $("cp-mused").textContent = f(m.used);
  $("cp-mnote").textContent = m.ok ? `Kotak P account · ${m.at || ""}` : `Kotak P account · ${m.msg || "not available"}`;
}
setInterval(askMargin, 30000);
setTimeout(askMargin, 1500);


// ── graphs column: MTM + CE/PE imbalance, always visible, refreshed every 5 s ──
S.gpMins = LS.get("gpMins", 1);
$("gp-mins").innerHTML = [1, 2, 5, 10, 15, 30, 60].map((n) => `<option value="${n}">${n} min</option>`).join("");
$("gp-mins").value = S.gpMins;
$("gp-mins").onchange = (e) => { S.gpMins = +e.target.value; LS.set("gpMins", S.gpMins); askInline(); };
function askInline() {
  if (getComputedStyle($("graphpane")).display === "none") return;
  send({ op: "mtm_hist", mins: S.gpMins, inst: "all", kind: "pnl", tag: "gp-pnl", req: ++S.req });
  send({ op: "mtm_hist", mins: S.gpMins, inst: "all", kind: "imb", tag: "gp-imb", req: ++S.req });
}
const hmIST = (t) => new Date(t * 1000).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" });
S.gpHist = { "gp-pnl": [], "gp-imb": [] };
// redraw the side charts whenever their box changes size (window resize, panel drag)
if (window.ResizeObserver) {
  // side by side or stacked: whichever gives each chart a shape nearer 2:1 in the panel's actual size
  new ResizeObserver(() => {
    const gp = $("graphpane"), w = gp.clientWidth, h = gp.clientHeight;
    if (!w || !h) return;
    const off = (a) => Math.abs(Math.log(Math.max(a, 0.05) / 2));
    const PAD = 24, HEAD = 62;                         // card padding / title + note line, per card
    const side = (w - 10) / 2 - PAD, sideH = h - HEAD, stackH = (h - 10) / 2 - HEAD, stackW = w - PAD;
    gp.classList.toggle("side", sideH > 0 && (stackH <= 40 || off(side / sideH) < off(stackW / stackH)));
  }).observe($("graphpane"));
  const ro = new ResizeObserver(() => { if (!S.book) return; for (const t of ["gp-pnl", "gp-imb"]) if (S.gpHist && S.gpHist[t]) drawInline(t); });
  ["gp-mtm", "gp-imb"].forEach((id) => $(id) && ro.observe($(id)));
}
function onInlineGraph(m) {
  S.gpHist[m.tag] = m.points || [];
  drawInline(m.tag);
}
// live numbers right now (same formulas as the engine's 5-second samples)
function liveNow() {
  let tot = 0, ce = 0, pe = 0;
  for (const p of S.book.positions) {
    const r = posPnl(p);
    tot += r.total;
    if (p.net < 0 && r.ltp != null) {
      if (/CE$/.test(p.symbol)) ce += r.ltp * -p.net * p.mult;
      else if (/PE$/.test(p.symbol)) pe += r.ltp * -p.net * p.mult;
    }
  }
  return { tot, ce, pe, imb: ce + pe > 0 ? ((ce - pe) / (ce + pe)) * 100 : null };
}
function drawInline(tag) {
  let pts = S.gpHist[tag].slice();
  if (S.book.positions.length) {                         // live last point, moves with every tick
    const now = Date.now() / 1000, L = liveNow();
    if (tag === "gp-pnl") pts.push([now, L.tot]);
    else if (L.imb != null) pts.push([now, L.imb, L.ce, L.pe]);
  }
  const m = { tag, points: pts };
  const box = m.tag === "gp-pnl" ? $("gp-mtm") : $("gp-imb");
  const note = m.tag === "gp-pnl" ? $("gp-mtm-note") : $("gp-imb-note");
  const bw = box.clientWidth, bh = box.clientHeight;
  if (pts.length < 2) {
    box.innerHTML = `<p class="gp-empty">${m.tag === "gp-pnl" ? "No trades yet today." : "Shows once you are short calls and/or puts."}</p>`;
    note.textContent = "";
    return;
  }
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const span = (xs[xs.length - 1] - xs[0]) / 60;
  const tip = m.tag === "gp-pnl"
    ? (i) => [hms(xs[i]), `P&L ${fr(ys[i])}`]
    : (i) => [hms(xs[i]), `Imbalance ${ys[i] > 0 ? "+" : ""}${ys[i].toFixed(1)}%`,
              ...(pts[i][2] != null ? [`CE ${nf2.format(pts[i][2])} · PE ${nf2.format(pts[i][3])}`] : [])];
  if (bw < 60 || bh < 40) return;                       // card too small to draw in (redrawn on resize)
  setChart(box, svgChart(xs, ys, { W: bw, H: bh,
                                   xfmt: hmIST, xticks: Math.max(1, Math.min(5, Math.floor(span))), now: true, tip,
                                   nowFmt: m.tag === "gp-pnl" ? (v) => nf.format(Math.round(v)) : (v) => v.toFixed(1) + "%",
                                   label: m.tag === "gp-pnl" ? "session P&L" : "CE/PE imbalance %" }));
  const last = pts[pts.length - 1];
  if (m.tag === "gp-pnl") {
    note.innerHTML = `Now <b class="${last[1] >= 0 ? "up-t" : "dn-t"}">${fr(last[1])}</b> · High <b class="up-t">${fr(Math.max(...ys))}</b> · Low <b class="dn-t">${fr(Math.min(...ys))}</b>`;
  } else {
    note.innerHTML = `Now <b>${last[1] > 0 ? "+" : ""}${last[1].toFixed(1)}%</b> · CE <b>${nf2.format(last[2])}</b> · PE <b>${nf2.format(last[3])}</b>` +
      ` <span class="dim">(above 0 = calls heavier)</span>`;
  }
}
setInterval(askInline, 5000);
setTimeout(askInline, 1200);
// redraw the live tail at most 4 times a second, only while positions are moving
setInterval(() => {
  if (!S.gpDirty || getComputedStyle($("graphpane")).display === "none") return;
  S.gpDirty = false;
  drawInline("gp-pnl");
  drawInline("gp-imb");
}, 250);


// ── ATM straddle (call + put at the money) per instrument, nearest expiry, from the engine once a second ──
function onAtm(v) {
  S.atmNow = v;
  if (S.ga && S.ga.hist && !$("graph").hidden && $("ga-name")) drawAtm();   // chart moves once a second
  for (const [n, a] of Object.entries(v)) {
    const txt = `ATM ${a[0]} · ${nf2.format(a[1])}`;
    if ($("ta-" + n)) $("ta-" + n).textContent = txt;
    if ($("ma-" + n)) $("ma-" + n).textContent = txt;
  }
}


// ── ATM chart: ATM straddle premium through the day (nearest expiry), per instrument ──
S.ga = { mins: LS.get("atmMins", 1), name: null };
$("btn-atm").onclick = () => {
  S.ga.name = S.cur.name;
  $("g-title").textContent = "ATM chart · straddle premium (call + put), nearest expiry";
  $("g-tools").innerHTML = `<select id="ga-name">${Object.values(S.prods).map((p) => `<option value="${p.name}">${p.label}</option>`).join("")}</select>` +
    ` One point per <select id="ga-mins">${[1, 2, 5, 10, 15, 30, 60].map((n) => `<option value="${n}">${n} min</option>`).join("")}</select>`;
  $("ga-name").value = S.ga.name;
  $("ga-mins").value = S.ga.mins;
  $("ga-name").onchange = (e) => { S.ga.name = e.target.value; S.ga.hist = null; askAtmHist(); };
  $("ga-mins").onchange = (e) => { S.ga.mins = +e.target.value; LS.set("atmMins", S.ga.mins); askAtmHist(); };
  $("g-body").innerHTML = `<p class="g-note">Loading…</p>`;
  $("g-note").textContent = "";
  $("graph").hidden = false;
  askAtmHist();
  clearInterval(S.gTimer);
  S.gTimer = setInterval(askAtmHist, 5000);
};
function askAtmHist() { send({ op: "atm_hist", name: S.ga.name, mins: S.ga.mins, req: ++S.req }); }
function onAtmHist(m) {
  if ($("graph").hidden || !$("ga-name") || m.name !== S.ga.name) return;
  S.ga.hist = m;
  drawAtm();
}
function drawAtm() {
  const m = S.ga.hist;
  if (!m || m.name !== S.ga.name) return;
  const pts = (m.points || []).slice();
  const live = (S.atmNow || {})[m.name];                  // live last point (engine sends it every second)
  if (live) pts.push([Date.now() / 1000, live[1], live[0]]);
  if (pts.length < 2) {
    $("g-body").innerHTML = `<p class="g-note">Not enough points yet — the engine stores the ATM straddle every 5 s.</p>`;
    $("g-note").textContent = "";
    return;
  }
  const hm = (t) => new Date(t * 1000).toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" });
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const lo = Math.min(...ys);
  const spanMin = (xs[xs.length - 1] - xs[0]) / 60;
  setChart($("g-body"), svgChart(xs, ys, { xfmt: hm, label: "ATM straddle", level: true, now: true,
                                       xticks: Math.max(1, Math.min(6, Math.floor(spanMin))),
                                       tip: (i) => [hms(xs[i]), `Straddle ${nf2.format(ys[i])}`, `ATM strike ${pts[i][2]}`] }));
  const strikes = [...new Set(pts.map((p) => p[2]))];
  const last = pts[pts.length - 1];
  const mx = Math.max(...ys), imx = ys.indexOf(mx), imn = ys.indexOf(lo);
  $("g-note").innerHTML = `Now <b>${nf2.format(last[1])}</b> (ATM ${last[2]}) · High <b>${nf2.format(mx)}</b> at ${hm(xs[imx])}` +
    ` · Low <b>${nf2.format(lo)}</b> at ${hm(xs[imn])} · ${pts.length} points, one per ${m.mins} min` +
    (strikes.length > 1 ? `<br><span class="dim">ATM strike moved during the day: ${strikes.join(" → ")}</span>` : "");
}

// ── EXIT row: X % of the CURRENT instrument now, or at a time (the timer runs on the VPS) ──
S.timers = [];
const pctOf = (f) => Math.round(f * 100);
function timerFor(name, frac) {
  return S.timers.find((x) => x.name === name && Math.abs(x.frac - frac) < 1e-9 && x.state === "armed");
}
function paintExitBar() {
  const n = S.cur.name;
  if (!n || !$("exitbar")) return;
  $("xb-name").textContent = (S.prods[n] || {}).label ? S.prods[n].label.toUpperCase() : n;
  document.querySelectorAll("#exitbar .xg").forEach((g) => {
    const t = timerFor(n, +g.dataset.frac), clk = g.querySelector(".xg-clk");
    clk.classList.toggle("armed", !!t);
    g.querySelector(".xg-at").textContent = t ? t.at : "";
    clk.title = t ? `Exit ${pctOf(t.frac)}% ${n} at ${t.at} IST (${t.mode}) - click to change or clear`
                  : `Exit ${pctOf(+g.dataset.frac)}% at a set time`;
  });
  const others = S.timers.filter((x) => x.state === "armed" && x.name !== n);
  $("xb-note").textContent = others.length
    ? "also armed: " + others.map((x) => `${(S.prods[x.name] || {}).label || x.name} ${pctOf(x.frac)}% @ ${x.at}`).join(" · ") : "";
}
function onTimers(items) {
  const before = new Map(S.timers.map((x) => [x.id, x.state]));
  S.timers = items;
  for (const x of items) {                                  // a timer just finished: say so, big
    const was = before.get(x.id);
    if (was && was !== x.state && (x.state === "done" || x.state === "failed"))
      toast(`TIMER ${x.at} · Exit ${pctOf(x.frac)}% ${x.name}: ${x.result}`, x.state === "failed", x.state === "failed" ? "bad" : "ok");
  }
  paintExitBar();
  if (!$("xpop").hidden) xpopPaint();
}
document.querySelectorAll("#exitbar .xg").forEach((g) => {
  const frac = +g.dataset.frac;
  g.querySelector(".xg-go").onclick = () => {
    if (send({ op: "exit_inst", name: S.cur.name, frac, req: ++S.req }) && S.mode === "LIVE")
      toast(`Exit ${pctOf(frac)}% ${S.cur.name} sent - checking Kotak…`, false, "wait");
  };
  g.querySelector(".xg-clk").onclick = (e) => { e.stopPropagation(); xpopOpen(g, frac); };
});
S.xp = null;
function xpopOpen(g, frac) {
  if (S.xp && S.xp.frac === frac && !$("xpop").hidden) { $("xpop").hidden = true; return; }
  S.xp = { frac, name: S.cur.name };
  const r = g.querySelector(".xg-clk").getBoundingClientRect(), pop = $("xpop");
  pop.hidden = false;
  const w = pop.offsetWidth;
  pop.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + "px";
  const below = r.bottom + 6, h = pop.offsetHeight;
  pop.style.top = (below + h > window.innerHeight - 8 ? r.top - h - 6 : below) + "px";
  const t = timerFor(S.xp.name, frac);
  $("xp-time").value = t ? t.at : new Date().toLocaleTimeString("en-GB", { timeZone: "Asia/Kolkata" });
  xpopPaint();
  $("xp-time").focus();
  $("xp-time").select();
}
function xpopPaint() {
  if (!S.xp) return;
  const t = timerFor(S.xp.name, S.xp.frac);
  $("xp-t").textContent = `Exit ${pctOf(S.xp.frac)}% Time (24h)`;
  $("xp-clear").hidden = !t;
  $("xp-n").textContent = (t ? `armed: ${t.at} IST · ` : "") + `${(S.prods[S.xp.name] || {}).label || S.xp.name} · ${S.mode}` +
    (S.mode === "LIVE" ? " (exits what Kotak holds)" : "");
}
$("xp-set").onclick = () => {
  if (!S.xp) return;
  send({ op: "timer_set", name: S.xp.name, frac: S.xp.frac, at: $("xp-time").value.trim(), req: ++S.req });
  $("xpop").hidden = true;
};
$("xp-clear").onclick = () => {
  if (!S.xp) return;
  send({ op: "timer_clear", name: S.xp.name, frac: S.xp.frac, req: ++S.req });
  $("xpop").hidden = true;
};
$("xp-time").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("xp-set").click(); }
  if (e.key === "Escape") $("xpop").hidden = true;
});
document.addEventListener("mousedown", (e) => {
  if (!$("xpop").hidden && !e.target.closest("#xpop") && !e.target.closest(".xg-clk")) $("xpop").hidden = true;
});

// ── Logs: the engine's append-only audit, newest first ──
S.lg = { timer: null };
const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
async function lgLoad() {
  const day = $("lg-day").value;
  if (!day) return;
  let rows = [];
  try { rows = await (await fetch("/audit/" + day, { credentials: "include" })).json(); } catch { return; }
  const q = $("lg-q").value.trim().toLowerCase(), k = $("lg-kind").value;
  const pick = rows.filter((r) => (!k || (k === "order" ? ["order", "fill", "order-status", "modify", "cancel"].includes(r.kind) : r.kind === k))
    && (!q || JSON.stringify(r).toLowerCase().includes(q)));
  const detail = (r) => {
    const x = { ...r }; delete x.t; delete x.kind; delete x.user; delete x.what;
    if (x.op) { x.op = undefined; }
    return Object.entries(x).filter(([, v]) => v !== undefined && v !== "" && !(Array.isArray(v) && !v.length))
      .map(([a, v]) => `${a}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ");
  };
  $("lg-tab").querySelector("tbody").innerHTML = pick.length ? pick.slice(0, 1500).map((r) => `<tr class="lg-${esc(r.kind)}">
    <td class="mono">${esc(r.t)}</td><td class="l">${esc(r.kind)}</td><td class="l dim">${esc((r.user || "").split("@")[0])}</td>
    <td class="l"><b>${esc(r.what || r.op || "")}</b></td><td class="l detail">${esc(detail(r))}</td></tr>`).join("")
    : `<tr><td class="empty">No entries${q || k ? " for this filter" : ""}.</td></tr>`;
}
$("btn-logs").onclick = async () => {
  $("logs").hidden = false;
  let days = [];
  try { days = await (await fetch("/audit", { credentials: "include" })).json(); } catch { /* shown below */ }
  $("lg-day").innerHTML = days.map((d) => `<option value="${d}">${fmtDate(d)}</option>`).join("") || `<option value="">no logs yet</option>`;
  lgLoad();
  clearInterval(S.lg.timer);
  S.lg.timer = setInterval(() => { if ($("logs").hidden) clearInterval(S.lg.timer); else if ($("lg-day").selectedIndex === 0) lgLoad(); }, 3000);
};
$("lg-day").onchange = lgLoad; $("lg-kind").onchange = lgLoad; $("lg-q").oninput = lgLoad;

// every button press / list choice goes to the audit log (who pressed what, in which mode)
document.addEventListener("click", (e) => {
  const b = e.target.closest && e.target.closest("button, .tk, .ip-item, .badge");
  if (!b || b.closest("#logs")) return;
  const what = (b.id ? b.id + ": " : "") + (b.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60);
  send({ op: "ui", what });
}, true);
document.addEventListener("change", (e) => {
  const el = e.target;
  if (!el || !el.id || el.closest("#logs")) return;
  send({ op: "ui", what: `${el.id} = ${el.type === "checkbox" ? el.checked : el.value}` });
}, true);

connect();
