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
const fr = (v) => (v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : "") + nf2.format(v));
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
      case "ack": toast(m.msg || (m.ok ? "done" : "failed"), !m.ok); break;
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
    S.sym.set(r.ce, { symbol: r.cs, lot: m.lot, k: r.k, side: "ce" });
    S.sym.set(r.pe, { symbol: r.ps, lot: m.lot, k: r.k, side: "pe" });
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
function startOrder(tok, side, opts = {}) {
  const info = S.sym.get(tok) || posInfo(tok);
  if (!info) return toast("unknown contract", true);
  const lots = opts.lots || S.qtyLots;
  if (S.askQty || S.askPx || opts.ticket) return openTicket(tok, side, lots, info);
  if (lots > S.meta.confirm_lots) return openTicket(tok, side, lots, info, true);
  placeOrder(tok, side, lots, null, opts.source || (S.kb ? "keyboard" : "button"));
}

function posInfo(tok) {
  const p = S.book.positions.find((x) => x.token === tok);
  return p ? { symbol: p.symbol, lot: p.lot } : null;
}

function placeOrder(tok, side, lots, limit, source) {
  if (lots > S.meta.max_lots) return toast(`${lots} lots is over the cap of ${S.meta.max_lots}`, true);
  if (!send({ op: "order", token: tok, side, lots, limit, source, req: ++S.req })) toast("not connected", true);
}

function openTicket(tok, side, lots, info, forceConfirm) {
  const box = $("ticket-box");
  S.ticket = { tok, side, info, confirmed: false, forceConfirm: !!forceConfirm };
  box.className = "ticket " + (side === "B" ? "buy" : "sell");
  $("t-side").textContent = side === "B" ? "BUY" : "SELL";
  $("t-sym").textContent = info.symbol;
  $("t-lots").value = lots;
  $("t-pxrow").hidden = !S.askPx;
  const t = S.T.get(tok);
  $("t-px").value = "";
  if (S.askPx && t) $("t-px").placeholder = `empty = at the market (${fp(side === "B" ? t[ASK] : t[BID])})`;
  $("ticket").hidden = false;
  ticketPaint();
  const f = S.askQty || !S.askPx ? $("t-lots") : $("t-px");
  f.focus(); f.select();
}
function ticketPaint() {
  const k = S.ticket;
  if (!k) return;
  const t = S.T.get(k.tok);
  $("t-quote").textContent = t ? `LTP ${fp(t[LTP])} · Bid ${fp(t[BID])} × ${fq(t[BQ])} lots · Ask ${fp(t[ASK])} × ${fq(t[AQ])} lots` : "no quote yet";
  const lots = Math.max(0, +$("t-lots").value | 0);
  $("t-units").textContent = `= ${fq(lots * k.info.lot)} qty`;
  let w = "";
  if (lots > S.meta.max_lots) w = `Over the cap of ${S.meta.max_lots} lots — will be refused`;
  else if (lots > S.meta.confirm_lots) w = k.confirmed ? `Press Enter again to send ${lots} lots` : `${lots} lots — press Enter twice to confirm`;
  $("t-warn").textContent = w;
}
function closeTicket() { $("ticket").hidden = true; S.ticket = null; }
function submitTicket() {
  const k = S.ticket;
  const lots = +$("t-lots").value | 0;
  if (lots < 1) return toast("enter at least 1 lot", true);
  if (lots > S.meta.confirm_lots && !k.confirmed) { k.confirmed = true; ticketPaint(); return; }
  const px = S.askPx ? parseFloat($("t-px").value) : NaN;
  placeOrder(k.tok, k.side, lots, isFinite(px) && px > 0 ? px : null, S.kb ? "keyboard" : "button");
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
  if (posMoved) paintPnl();
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
      <td>${p.net ? `<button class="btn sm danger" data-x="exit">Exit</button> <button class="btn sm" data-x="add">Add</button>` : ""}</td>
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
  const pos = b.dataset.p === "pos";
  document.querySelectorAll(".dt").forEach((x) => x.classList.toggle("on", x === b));
  $("pos-table").hidden = !pos; $("tools-pos").hidden = !pos;
  $("ord-table").hidden = pos; $("tools-ord").hidden = pos;
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

connect();
