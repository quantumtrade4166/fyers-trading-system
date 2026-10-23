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
      case "risk": onRisk(m); break;
      case "ack":
        if (m.op === "mtm_hist") onMtm(m);
        else if (m.op === "settings_set") { onSettingsAck(m); toast(m.msg, !m.ok); }
        else toast(m.msg || (m.ok ? "done" : "failed"), !m.ok);
        break;
      case "pong": $("ping").textContent = `${(performance.now() - m.c).toFixed(0)} ms`; break;
    }
  };
}
const send = (o) => { if (S.ws && S.ws.readyState === 1) { S.ws.send(JSON.stringify(o)); return true; } return false; };
setInterval(() => send({ op: "ping", c: performance.now() }), 2000);

function setFeed(txt, cls) { $("feedtxt").textContent = txt; $("feed").className = "pill " + (cls || ""); }

function toast(msg, bad) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), bad ? 6000 : 3000);
}

// ── meta / controls ─────────────────────────────────────────────────────────
function onMeta(m) {
  S.meta = m;
  m.products.forEach((p) => (S.prods[p.name] = p));
  const name = S.cur.name || LS.get("inst", m.products[0].name);
  S.cur.strikes = LS.get("strikes", m.default_strikes);
  $("strikes").innerHTML = m.strikes.map((n) => `<option value="${n}">${n}</option>`).join("");
  $("strikes").value = S.cur.strikes;
  $("inst").innerHTML = m.products.map((p) => `<button data-n="${p.name}">${p.label}</button>`).join("");
  $("tickers").innerHTML = m.products.map((p) => `
    <div class="tk" data-n="${p.name}">
      <span class="n">${p.label.toUpperCase()}</span><span class="p" id="tp-${p.name}">—</span>
      <span class="c" id="tc-${p.name}">—</span><span class="s" id="ts-${p.name}"></span>
    </div>`).join("");
  $("qty").innerHTML = Array.from({ length: m.qty_lots_max }, (_, i) => `<option value="${i + 1}">${i + 1} lot${i ? "s" : ""}</option>`).join("");
  document.querySelectorAll("#inst button, .tk").forEach((b) => (b.onclick = () => pick(b.dataset.n)));
  pick(S.prods[name] ? name : m.products[0].name, true);
}

function underlyingFut(p, expiry) { return p.futures.find((f) => f.expiry >= expiry) || p.futures[p.futures.length - 1]; }

function pick(name, keepExpiry) {
  const p = S.prods[name];
  S.cur.name = name;
  LS.set("inst", name);
  const saved = keepExpiry ? LS.get("exp." + name, null) : null;
  S.cur.expiry = p.expiries.includes(saved) ? saved : p.expiries[0];
  $("expiry").innerHTML = p.expiries.map((e) => `<option value="${e}">${fmtDate(e)}</option>`).join("");
  $("expiry").value = S.cur.expiry;
  document.querySelectorAll("#inst button").forEach((b) => b.classList.toggle("on", b.dataset.n === name));
  document.querySelectorAll(".tk").forEach((b) => b.classList.toggle("on", b.dataset.n === name));
  S.qtyLots = LS.get("qty." + name, 1);
  paintQty();
  requestChain();
}

function requestChain() {
  $("rows").innerHTML = `<tr><td colspan="15" class="empty">Loading chain…</td></tr>`;
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
  const rows = m.rows;
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
      `<td class="q"></td><td class="q"></td><td class="ba"></td><td class="ba"></td><td class="c"></td>` +
      `<td class="act" data-tok="${r.ce}">${BS}</td><td class="ltp"></td>` +
      `<td class="k">${fp(r.k, r.k % 1 ? 1 : 0)}</td>` +
      `<td class="ltp"></td><td class="act" data-tok="${r.pe}">${BS}</td>` +
      `<td class="c"></td><td class="ba"></td><td class="ba"></td><td class="q"></td><td class="q"></td>`;
    const td = tr.children;
    S.cells.set(r.ce, { oi: td[0], vol: td[1], bid: td[2], ask: td[3], chg: td[4], ltp: td[6] });
    S.cells.set(r.pe, { ltp: td[8], chg: td[10], bid: td[11], ask: td[12], vol: td[13], oi: td[14] });
    td[6].dataset.tok = r.ce; td[8].dataset.tok = r.pe;
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
    for (let i = 0; i < 7; i++) c[i].classList.toggle("itm", ceItm);
    for (let i = 8; i < 15; i++) c[i].classList.toggle("itm", peItm);
  }
}

function paint(tok, flash, prevLtp) {
  const c = S.cells.get(tok);
  const t = S.T.get(tok);
  if (!c || !t) return;
  const l = t[LTP];
  set(c.ltp, fp(l));
  const ch = pct(l, t[PC]);
  set(c.chg, ch == null ? "—" : (ch > 0 ? "+" : "") + ch.toFixed(2));
  const dir = ch == null ? "" : ch > 0 ? "up" : ch < 0 ? "dn" : "";
  c.ltp.classList.toggle("up", dir === "up"); c.ltp.classList.toggle("dn", dir === "dn");
  c.chg.classList.toggle("up", dir === "up"); c.chg.classList.toggle("dn", dir === "dn");
  set(c.bid, fp(t[BID])); set(c.ask, fp(t[ASK]));
  set(c.vol, fq(t[VOL])); set(c.oi, fq(t[OI]));
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
  if (S.askQty || (S.askPx && legs.length === 1) || opts.ticket) return openTicket(legs, lots);
  if (lots > S.meta.confirm_lots) return openTicket(legs, lots);
  sendLegs(full, lots, null, opts.source || (S.kb ? "keyboard" : "button"));
}

function hedgeTok(tok) {
  const info = S.sym.get(tok);
  if (!info || !S.chain) return null;
  const rows = S.chain.rows;
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
  if (legs.length === 1) ok = send({ op: "order", token: legs[0].tok, side: legs[0].side, lots, limit, source, req: ++S.req });
  else ok = send({ op: "basket", legs: legs.map((l) => ({ token: l.tok, side: l.side, lots })), source, req: ++S.req });
  if (!ok) toast("not connected", true);
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

$("exitall").onclick = () => send({ op: "exit_all", req: ++S.req });
document.querySelectorAll("#tools-pos button[data-frac]").forEach((b) =>
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
      const k = m.rows[nearestIdx(m.rows, px)].k;
      if (k !== S.atm) layout(); else { shade(); paintUnderlying(); }
    }
  }
  if (posMoved) { paintPnl(); paintRiskSum(); }
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
  }
}

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
  const s = $("ts-" + m.name);
  if (s) s.textContent = syn == null ? "" : `Syn ${fp(syn, 0)}`;
}

// ── positions / orders ──────────────────────────────────────────────────────
function onBook(m) {
  S.book = m;
  S.posTok = new Set(m.positions.map((p) => p.token));
  $("npos").textContent = m.positions.filter((p) => p.net).length;
  $("nord").textContent = m.orders.filter((o) => o.status === "OPEN").length || m.orders.length;
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

function renderPositions() {
  const rows = S.book.positions;
  const tb = $("pos-rows");
  if (!rows.length) { tb.innerHTML = `<tr><td colspan="7" class="empty">No positions yet.</td></tr>`; paintPnl(); return; }
  tb.innerHTML = rows.map((p) => {
    const lots = p.net / p.lot;
    return `<tr class="${p.net ? "" : "flat"}" data-tok="${p.token}">
      <td class="l">${p.symbol}</td>
      <td class="${p.net > 0 ? "up-t" : p.net < 0 ? "dn-t" : ""}">${p.net ? (lots > 0 ? "+" : "") + lots + " lot" + (Math.abs(lots) === 1 ? "" : "s") : "flat"}</td>
      <td>${p.net ? fp(p.avg) : "—"}</td><td class="pl-ltp">—</td><td class="pl-open">—</td><td>${fr(p.realized)}</td>
      <td>${p.net ? `<button class="btn sm danger" data-x="exit">Exit</button> <button class="btn sm" data-x="add">Add</button>` +
        (/(CE|PE)$/.test(p.symbol) ? ` <button class="btn sm" data-x="away" title="Roll this leg further out of the money">Move away</button> <button class="btn sm" data-x="close" title="Roll this leg toward the money">Move close</button>` : "") : ""}</td>
    </tr>`;
  }).join("") + `<tr class="tot"><td class="l">Total</td><td></td><td></td><td></td><td id="pl-tot-open"></td><td id="pl-tot-real"></td><td></td></tr>`;
  paintPnl();
}

$("pos-rows").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-x]");
  if (!b) return;
  const tok = +b.closest("tr").dataset.tok;
  const p = S.book.positions.find((x) => x.token === tok);
  if (!p) return;
  if (b.dataset.x === "exit") send({ op: "exit", token: tok, req: ++S.req });
  else if (b.dataset.x === "away" || b.dataset.x === "close")
    send({ op: "move", token: tok, steps: +$("move-n").value, away: b.dataset.x === "away", req: ++S.req });
  else startOrder(tok, p.net > 0 ? "B" : "S", { source: "add" });
});

function paintPnl() {
  let total = 0, open = 0, real = 0;
  for (const p of S.book.positions) {
    const r = posPnl(p);
    total += r.total; open += r.open; real += p.realized;
    const tr = document.querySelector(`#pos-rows tr[data-tok="${p.token}"]`);
    if (tr) {
      tr.querySelector(".pl-ltp").textContent = fp(r.ltp);
      const o = tr.querySelector(".pl-open");
      o.textContent = p.net ? fr(r.open) : "—";
      o.className = "pl-open " + (r.open > 0 ? "up-t" : r.open < 0 ? "dn-t" : "");
    }
  }
  const to = $("pl-tot-open"), tr_ = $("pl-tot-real");
  if (to) { to.textContent = fr(open); to.className = open > 0 ? "up-t" : open < 0 ? "dn-t" : ""; }
  if (tr_) { tr_.textContent = fr(real); tr_.className = real > 0 ? "up-t" : real < 0 ? "dn-t" : ""; }
  const el = $("pnl");
  el.textContent = fr(total);
  el.className = "val mono " + (total > 0 ? "up-t" : total < 0 ? "dn-t" : "");
}

function renderOrders() {
  let rows = S.book.orders;
  if (S.ofilter === "open") rows = rows.filter((o) => o.status === "OPEN");
  else if (S.ofilter === "done") rows = rows.filter((o) => o.status !== "OPEN");
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
  $("risk-pane").hidden = p !== "risk";
}));

// green / red edge on chain prices where I hold a position
function markPositions() {
  document.querySelectorAll("#rows td.pos-long, #rows td.pos-short").forEach((td) => td.classList.remove("pos-long", "pos-short"));
  for (const p of S.book.positions) {
    if (!p.net) continue;
    const c = S.cells.get(p.token);
    if (c) c.ltp.classList.add(p.net > 0 ? "pos-long" : "pos-short");
  }
}

// ── status / footer / clock ─────────────────────────────────────────────────
function onStatus(m) {
  const f = m.feed || {};
  $("mode").textContent = m.mode || "PAPER";
  $("mode").className = "badge " + (m.mode === "LIVE" ? "live" : "paper");
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
  const rows = m.rows;
  const ai = rows.findIndex((r) => r.k === S.atm);
  if (ai < 0) return;
  const n = S.cur.strikes;
  const pairs = [];
  if (S.view === "straddle") {
    const g = strikeGap();
    for (const r of S.visRows) {
      const off = rows.indexOf(r) - ai;
      if (off % g === 0) pairs.push({ a: r.ce, b: r.pe, ka: r.k, kb: r.k });
    }
    $("cb-h1").textContent = "Strike"; $("cb-h2").textContent = "";
  } else {
    const g = strikeGap();
    for (let i = 0; i <= n * g; i += g) {
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
  $("nrisk").textContent = m.armed ? "on" : m.fired ? "fired" : "off";
  $("nrisk").style.color = m.armed ? "var(--up)" : m.fired ? "var(--down)" : "";
  paintRiskSum();
}
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
  $("rk-sum").innerHTML = `Short CE premium <b>${nf2.format(ce)}</b> (${nce} lots) · Short PE premium <b>${nf2.format(pe)}</b> (${npe} lots)` +
    ` · Total <b>${nf2.format(tot)}</b> · CE/PE imbalance <b>${imb == null ? "—" : imb.toFixed(1) + "%"}</b> · Open legs <b>${legs}</b>`;
}

setView(S.view);


// ── Stage 3b: settings, payoff graph, MTM graph ────────────────────────────
S.settings = null;
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => closeModal(b.dataset.close)));
["settings", "graph"].forEach((id) => $(id).addEventListener("click", (e) => { if (e.target.id === id) closeModal(id); }));
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("settings").hidden) closeModal("settings");
  if (!$("graph").hidden) closeModal("graph");
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
  $("s-gaps").innerHTML = Object.values(S.prods).map((p) => `<label>${p.label}<select data-gap="${p.name}">` +
    [1, 2, 3, 4, 5, 10].map((g) => `<option value="${g}">${g === 1 ? "every strike" : "every " + g + "th"}</option>`).join("") +
    `</select></label>`).join("");
  document.querySelectorAll("#s-gaps select").forEach((s) => (s.value = (v.strike_gap || {})[s.dataset.gap] || 1));
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
  if (m.ok) { setTimeout(() => closeModal("settings"), 500); buildCombo(); }
}
function strikeGap() {
  const v = S.settings || (S.meta && S.meta.settings) || {};
  return ((v.strike_gap || {})[S.cur.name]) || 1;
}

// ── small SVG chart helper (one series, zero line, x labels) ──
function svgChart(xs, ys, o = {}) {
  const W = 820, H = 300, L = 64, R = 16, T = 14, B = 30;
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys);
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
    ${mark}${xl}</svg>`;
}

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
  $("g-body").innerHTML = svgChart(xs, ys, { mark: spot, markLabel: `now ${fp(spot, 0)}`, label: "payoff at expiry" });
  $("g-note").innerHTML = `Max profit in range <b class="up-t">${fr(Math.max(...ys))}</b> · Max loss in range <b class="dn-t">${fr(Math.min(...ys))}</b>` +
    ` · Breakeven ${be.length ? be.map((b) => `<b>${nf.format(Math.round(b))}</b>`).join(" / ") : "<b>none in range</b>"}` +
    ` · Open P&L now <b>${fr(now)}</b><br><span class="dim">${legs.length} open leg(s) at their average prices, ±5 % around the strikes. ` +
    `Closed trades and time value are not included; the loss can be larger outside this range.</span>`;
  $("graph").hidden = false;
};

// ── MTM graph: today's Session P&L, one point per N minutes ──
S.g = { mins: LS.get("mtmMins", 1), inst: "all" };
$("btn-mtm").onclick = () => {
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
  $("g-body").innerHTML = svgChart(xs, ys, { xfmt: hm, label: "session P&L over time", xticks: Math.max(1, Math.min(6, Math.floor(span))) });
  const mx = Math.max(...ys), mn = Math.min(...ys), ih = ys.indexOf(mx), il = ys.indexOf(mn);
  $("g-note").innerHTML = `Now <b class="${ys[ys.length - 1] >= 0 ? "up-t" : "dn-t"}">${fr(ys[ys.length - 1])}</b> · High <b class="up-t">${fr(mx)}</b> at ${hm(xs[ih])}` +
    ` · Low <b class="dn-t">${fr(mn)}</b> at ${hm(xs[il])} · ${pts.length} points, one per ${m.mins} min`;
}

connect();
