"""
live_kotak.py — REAL orders on the Kotak D account, mirroring the paper engine.
==============================================================================

⚠️ LIVE MONEY. The paper engine (engine.py / DayMachine) makes every decision; this
module only copies its position changes onto Kotak D with real NRML orders:

    paper SELL opens   ->  SELL lots x lot_size of the same contract on Kotak D
    paper BUY closes   ->  BUY back exactly what Kotak D still holds (any exit reason)

Control (owner only, from the dashboard -> data/live_state/live_control_KOTAK_D.json):
    {"armed": false, "lots": 1, "kill": false}
  * armed  - only NEW paper entries are mirrored; arming while a paper trade is already
             open does NOT chase it (no late entry at a worse price)
  * lots   - read at each entry; a change never resizes an open position
  * kill   - buy back anything open NOW, disarm, no more entries today

Never naked / never stuck:
  * every order is a marketable LIMIT priced off Kotak's own depth (kotak_executor),
    clamped inside the exchange price band; unfilled -> cancel + re-price + retry
  * a BUY-back that cannot complete is retried every few seconds (step loop) until
    Kotak confirms it, and the dashboard shows it as an alarm
  * margin rejection on an entry -> no position, entries halted for the day
  * 15:16 safety square-off of anything still open
  * restart: the open quantity is rebuilt from today's COMPLETE Kotak orders carrying
    our tag prefix (own book, never positions(), which can include manual trades)

Account: Kotak D (env KOTAK_*_D, kotak_auth.login(d=True)) — owner: "call it Kotak D".
Order tag prefix: "stdk" (distinct from every strangle mirror).

Speed (26-Oct: the mirror lagged paper by 9 s and an unfilled limit sat 66 s):
  * prepare(contract) resolves the Kotak contract + price band in the background the
    moment a setup exists, so nothing is looked up when the signal fires
  * the first order is priced off our own live Kite tick (no broker call before send)
  * every status / cancel / quote call has a hard timeout; an unfilled order is
    cancelled and re-priced after a few seconds, never left resting
  * each fill records ms timings: decision -> sent -> Kotak ack -> fill seen
"""
import json
import math
import time
import threading
import datetime as dt
import importlib.util
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
STATE_DIR = ROOT / "data" / "live_state"
RESULTS = ROOT / "data" / "results"
CONTROL = STATE_DIR / "live_control_KOTAK_D.json"
TAG = "stdk"
FREEZE_QTY = 1800               # NIFTY max quantity per order (larger orders are sliced)
LAST_ENTRY = dt.time(15, 10)
SAFETY_SQUAREOFF = dt.time(15, 16)

TICK = 0.05
FIRST_CUSHION = 0.01            # first order: 1% through our own LTP (fills at the touch)
LTP_FRESH_S = 2.0               # our tick must be this fresh to price the first order
WAIT_FIRST_S = 3.0              # a marketable limit on an ATM NIFTY option fills in ms
WAIT_RETRY_S = 4.0
CALL_TIMEOUT_S = 2.5            # status / cancel / quote calls never block longer
CONFIRM_S = 6.0                 # after a cancel: time to learn the order's final state
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kotakd")


def _round_tick(px: float) -> float:
    return round(round(float(px) / TICK) * TICK, 2)


def _ms() -> int:
    return int(time.time() * 1000)


def round_trips(fills: list, date: str, ltp_by_symbol: dict | None = None) -> list:
    """Kotak D fills -> one row per round trip (like the paper Trades table).
    Fills carry the paper trade id from 26-Oct; older fills are paired FIFO per symbol."""
    from backtesting.options_pivot_intraday.costs import statutory_costs
    trips, open_by = [], {}
    for f in fills:
        sym = f.get("symbol") or f.get("trading_symbol")
        key = f.get("trade_id") or sym
        q, px = int(f.get("qty") or 0), float(f.get("price") or f.get("avg_price") or 0)
        t = str(f.get("time") or "")[-8:]
        if str(f.get("side")).upper().startswith("S"):
            tr = open_by.get(key)
            if tr is None or tr["buy_qty"]:
                tr = {"trade_id": f.get("trade_id"), "symbol": sym, "sell_qty": 0, "sell_val": 0.0,
                      "sell_time": t, "buy_qty": 0, "buy_val": 0.0, "buy_time": None, "reason": None}
                trips.append(tr)
                open_by[key] = tr
            tr["sell_qty"] += q
            tr["sell_val"] += q * px
        else:
            tr = open_by.get(key)
            if tr is None:
                continue
            tr["buy_qty"] += q
            tr["buy_val"] += q * px
            tr["buy_time"] = t
            tr["reason"] = f.get("reason") or tr["reason"]
            if tr["buy_qty"] >= tr["sell_qty"]:
                open_by.pop(key, None)
    out = []
    for tr in trips:
        sq, bq = tr["sell_qty"], tr["buy_qty"]
        if not sq:
            continue
        s_avg = tr["sell_val"] / sq
        b_avg = tr["buy_val"] / bq if bq else None
        is_open = bq < sq
        ltp = (ltp_by_symbol or {}).get(tr["symbol"]) if is_open else None
        gross = (s_avg - b_avg) * bq if bq else 0.0
        if is_open and ltp:
            gross += (s_avg - ltp) * (sq - bq)
        exit_px = b_avg if not is_open else ltp
        ch = statutory_costs(s_avg, exit_px, sq, date)["total"] if exit_px else None
        out.append({"trade_id": tr["trade_id"], "symbol": tr["symbol"], "qty": sq,
                    "sell_time": tr["sell_time"], "sell_price": round(s_avg, 2),
                    "buy_time": None if is_open else tr["buy_time"],
                    "buy_price": None if is_open else round(b_avg, 2), "ltp": ltp, "open": is_open,
                    "reason": tr["reason"], "gross": round(gross, 2),
                    "charges": round(ch, 2) if ch is not None else None,
                    "net": round(gross - ch, 2) if ch is not None else None})
    return out


def link_paper(trips: list, paper_trades: list) -> list:
    """Attach the paper trade each Kotak D round trip copied: by trade id, else (fills from
    before 26-Oct carry none) the next unused paper trade on the same strike + type."""
    used = set()
    by_id = {t.get("trade_id"): t for t in paper_trades}
    for tr in trips:
        pt = by_id.get(tr.get("trade_id")) if tr.get("trade_id") else None
        if pt is None:
            for cand in paper_trades:
                tag = f"{int(cand['strike'])}{cand['opt_type']}"
                if cand["trade_id"] not in used and str(tr["symbol"]).endswith(tag):
                    pt = cand
                    break
        if pt is None:
            continue
        used.add(pt["trade_id"])
        tr.update(trade_id=tr.get("trade_id") or pt["trade_id"], paper_entry=pt.get("entry_price"),
                  paper_exit=pt.get("exit_price"), signal_ts=str(pt.get("signal_ts") or ""),
                  signal_close=pt.get("signal_close"), signal_ref=pt.get("signal_ref"),
                  origin=pt.get("origin"), expiry=str(pt.get("expiry") or "")[:10],
                  strike=pt.get("strike"), opt_type=pt.get("opt_type"),
                  reason=tr.get("reason") if tr.get("reason") not in (None, "entry") else pt.get("exit_reason"),
                  exit_close=pt.get("exit_close"), exit_st=pt.get("exit_st"))
    return trips


def realized_fifo(fills: list) -> float:
    """Gross realized P&L of short round trips, paired FIFO per symbol. (The old view
    averaged ALL sells against ALL buys across different contracts: on 26-Oct it showed
    about -9,100 for a true -5,766.)"""
    by_sym: dict = {}
    for f in fills:
        by_sym.setdefault(f.get("symbol") or f.get("trading_symbol"), []).append(f)
    total = 0.0
    for rows in by_sym.values():
        opens = []                                   # [qty, price] still short
        for f in rows:
            q, px = int(f.get("qty") or 0), float(f.get("price") or f.get("avg_price") or 0)
            if str(f.get("side")).upper().startswith("S"):
                opens.append([q, px])
                continue
            while q > 0 and opens:
                take = min(q, opens[0][0])
                total += (opens[0][1] - px) * take
                opens[0][0] -= take
                q -= take
                if opens[0][0] == 0:
                    opens.pop(0)
    return total


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def login_kotak_d():
    """Kotak D session (env KOTAK_*_D). Self-contained on purpose: the VPS copy of the
    shared kotak_auth.login() predates the D account, and that file is used by the live
    strangle mirrors, so it is NOT touched. Reuses its proven helpers (TOTP, response
    check, mobile-number variants). Never places an order."""
    import os
    auth = _load("live_trading_options/strangle_strategy/live/kotak_auth.py", "_stdir_kauth")  # loads .env
    from neo_api_client import NeoAPI
    req = ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET")
    creds = {k: os.getenv(k + "_D") for k in req}
    missing = [k + "_D" for k, v in creds.items() if not v]
    if missing:
        raise RuntimeError("Kotak D .env missing: " + ", ".join(missing))
    last = None
    for mob in auth._mobile_variants(creds["KOTAK_MOBILE"]):
        try:
            client = NeoAPI(environment="prod", access_token=None, neo_fin_key=None,
                            consumer_key=creds["KOTAK_CONSUMER_KEY"])
            r1 = client.totp_login(mobile_number=mob, ucc=creds["KOTAK_UCC"],
                                   totp=auth._totp_now(creds["KOTAK_TOTP_SECRET"]))
            if not auth._ok(r1):
                last = f"totp_login: {r1.get('error')}"
                continue
            r2 = client.totp_validate(mpin=creds["KOTAK_MPIN"])
            if not auth._ok(r2):
                last = f"totp_validate: {r2.get('error')}"
                continue
            return client
        except Exception as ex:
            last = f"{type(ex).__name__}: {ex}"
    raise RuntimeError(f"Kotak D login failed: {last}")


def read_control() -> dict:
    try:
        d = json.loads(CONTROL.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    lots = d.get("lots", 1)
    try:
        lots = max(0, int(lots))
    except Exception:
        lots = 1
    return {"armed": bool(d.get("armed", False)), "lots": lots, "kill": bool(d.get("kill", False)),
            "updated": d.get("updated"), "date": d.get("date")}


def write_control(**kw) -> dict:
    d = read_control()
    d.update({k: v for k, v in kw.items() if v is not None})
    d["updated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONTROL.with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(CONTROL)
    return d


class KotakDLive:
    """Mirrors the DayMachine's position onto Kotak D. All broker calls go through
    `ke` (kotak_executor) and `client`, so tests inject fakes."""

    def __init__(self, eng, ke=None, login=None, clock=None, sleep=time.sleep):
        self.e = eng
        self.ke = ke or _load("live_trading_options/strangle_strategy/live/kotak_executor.py", "_stdir_ke")
        self._login = login
        self.clock = clock or eng.clock
        self.sleep = sleep
        self.client = None
        self.login_error = None
        self.ctrl = read_control()
        self.halted = None                 # reason entries are halted for the day
        self.pos = None                    # {symbol, seg, token, qty, avg, trade_id, contract}
        self.fills = []                    # every fill today (own book)
        self.pending_exit = None           # exit we still owe the broker
        self.last_error = None
        self.mirrored = set()              # paper trade ids already sent (entry)
        self.closed = set()                # paper trade ids already closed
        self.skipped = set()               # paper trade ids deliberately NOT mirrored
        self.date = self.clock().strftime("%Y-%m-%d")
        self.book_file = RESULTS / f"{self.date}_kotak_d.json"
        self.prepared = {}                 # (expiry, strike, opt) -> {inst, lo, hi, at}
        self._prep_lock = threading.Lock()
        self._login_lock = threading.Lock()
        self.realized = 0.0                # FIFO realized gross, refreshed on every fill
        self.unsettled = {}                # order id -> info, state not yet confirmed by Kotak
        self.wait_first, self.wait_retry, self.confirm_s = WAIT_FIRST_S, WAIT_RETRY_S, CONFIRM_S
        self._ctx = {}                     # trade id / leg / reason stamped on each fill

    # ---------- plumbing ----------
    def event(self, kind, msg):
        self.e.event(kind, f"[KOTAK D] {msg}")

    def _timed(self, timeout, fn, *a, **kw):
        """Run a broker read/cancel with a hard deadline (raises FTimeout). The call may
        keep running in its worker thread; we just stop waiting for it."""
        return _POOL.submit(fn, *a, **kw).result(timeout=timeout)

    @staticmethod
    def _key(contract):
        expiry, strike, opt = contract
        return (str(expiry)[:10], int(strike), str(opt))

    def prepare(self, contract):
        """Resolve the Kotak contract and its price band BEFORE any signal (background).
        Idempotent; never raises."""
        k = self._key(contract)
        with self._prep_lock:
            if k in self.prepared:
                return self.prepared[k]
        try:
            if self.ensure_client() is None:
                return None
            inst = self._resolve(contract)
            lo = hi = None
            try:
                book = self._timed(CALL_TIMEOUT_S, self.ke.quote_book, self.client, inst.get("token"),
                                   inst["exchange_segment"])
                if book:
                    lo, hi = book.get("low_lpp"), book.get("high_lpp")
            except Exception:
                pass
            p = {"inst": inst, "lo": lo, "hi": hi, "at": self.clock().strftime("%H:%M:%S")}
            with self._prep_lock:
                self.prepared[k] = p
            return p
        except Exception as ex:
            self.event("WARN", f"prepare {k} failed (will resolve at fire time): {ex}")
            return None

    def _first_price(self, contract, side, inst):
        """Marketable limit from OUR live tick: 1% through the LTP, inside the band."""
        key = self.e.contract_key(tuple(contract)) if contract else None
        ltp = self.e.ltp_fresh(key, LTP_FRESH_S) if (key and hasattr(self.e, "ltp_fresh")) else None
        if not ltp:
            return None, "no fresh tick"
        sell = side.upper().startswith("S")
        px = _round_tick(ltp * (1 - FIRST_CUSHION) if sell else ltp * (1 + FIRST_CUSHION))
        p = self.prepared.get(self._key(contract)) if contract else None
        if p:
            if sell and p.get("lo"):
                px = max(px, _round_tick(p["lo"]))
            if not sell and p.get("hi"):
                px = min(px, _round_tick(p["hi"]))
        return max(px, TICK), f"tick {ltp}"

    def ensure_client(self):
        if self.client is not None:
            return self.client
        with self._login_lock:                   # prepare() runs in a background thread
            if self.client is not None:
                return self.client
            return self._login_now()

    def _login_now(self):
        try:
            if self._login is not None:
                self.client = self._login()
            else:
                self.client = login_kotak_d()
            self.login_error = None
            self.event("LIVE", "Kotak D login OK")
        except Exception as ex:
            self.login_error = f"{type(ex).__name__}: {ex}"
            self.event("ERROR", f"Kotak D login failed: {self.login_error}")
            self.client = None
        return self.client

    def _persist(self):
        try:
            RESULTS.mkdir(parents=True, exist_ok=True)
            self.book_file.write_text(json.dumps({
                "date": self.date, "pos": self.pos, "fills": self.fills, "halted": self.halted,
                "pending_exit": self.pending_exit, "last_error": self.last_error,
                "mirrored": sorted(self.mirrored), "closed": sorted(self.closed),
                "skipped": sorted(self.skipped), "unsettled": self.unsettled,
                "prepared": {f"{k[0]} {k[1]} {k[2]}": {"symbol": v["inst"].get("trading_symbol"),
                                                       "lo": v.get("lo"), "hi": v.get("hi"), "at": v.get("at")}
                             for k, v in self.prepared.items()}}, default=str), encoding="utf-8")
        except Exception as ex:
            print(f"  [kotak-d] persist failed: {ex}", flush=True)

    # ---------- restart recovery ----------
    def reconcile(self):
        """Rebuild the open quantity from today's COMPLETE orders carrying our tag."""
        try:
            prev = json.loads(self.book_file.read_text(encoding="utf-8"))
            self.mirrored = set(prev.get("mirrored", []))
            self.closed = set(prev.get("closed", []))
            self.skipped = set(prev.get("skipped", []))
            self.halted = prev.get("halted")
            self.unsettled = prev.get("unsettled") or {}
            saved_pos = prev.get("pos")
        except Exception:
            saved_pos = None
        if not (self.ctrl["armed"] or saved_pos or self.mirrored):
            return                                   # never traded today: no login needed
        c = self.ensure_client()
        if c is None:
            return
        try:
            fills = self.ke.strategy_fills(c, tag_prefix=TAG)
        except Exception as ex:
            self.event("ERROR", f"reconcile: cannot read Kotak order book: {ex}")
            return
        net, cost = {}, {}
        for f in fills:
            sym, q, px = f["trading_symbol"], int(f["qty"] or 0), float(f["avg_price"] or 0)
            side = str(f["side"]).upper()[:1]
            sgn = -1 if side == "S" else 1
            net[sym] = net.get(sym, 0) + sgn * q
            if side == "S":
                cost.setdefault(sym, [0, 0.0])
                cost[sym][0] += q
                cost[sym][1] += q * px
        self.fills = fills
        self.realized = realized_fifo(fills)
        open_syms = [s for s, q in net.items() if q < 0]
        if open_syms:
            sym = open_syms[0]
            q = -net[sym]
            avg = cost[sym][1] / cost[sym][0] if cost.get(sym) and cost[sym][0] else None
            base = saved_pos or {}
            self.pos = {**base, "symbol": sym, "qty": q, "avg": avg}
            self.event("RESUME", f"Kotak D open short rebuilt from own orders: {sym} qty {q} avg {avg}")
        else:
            self.pos = None
        self._persist()

    # ---------- the hook: called after every machine change ----------
    def sync(self):
        """Compare the paper machine's position with ours and act. Cheap when nothing changed."""
        now = self.clock()
        self.ctrl = read_control()
        if self.unsettled:
            if self.ensure_client() is not None:
                self._settle()
            if self.unsettled:
                self.last_error = f"waiting for Kotak to confirm order(s) {', '.join(self.unsettled)} — no new orders"
                return
        if self.ctrl.get("kill"):
            self.kill("KILL pressed")
            return
        if self.pending_exit:
            self._buy_back(self.pending_exit.get("reason", "retry"))
            return
        if now.time() >= SAFETY_SQUAREOFF and self.pos:
            self._buy_back("safety square-off 15:16")
            return
        paper = self.e.machine.position
        # paper closed (any reason) -> buy back what we hold for that trade
        if self.pos and (paper is None or paper["trade_id"] != self.pos.get("trade_id")):
            reason = "paper exit"
            for tr in self.e.machine.trades:
                if tr["trade_id"] == self.pos.get("trade_id") and tr.get("exit_reason"):
                    reason = tr["exit_reason"]
            self._buy_back(reason)
            return
        # paper opened a new trade -> sell, if armed and allowed
        if paper is not None and not self.pos:
            tid = paper["trade_id"]
            if tid in self.mirrored or tid in self.skipped:
                return
            why = None
            if not self.ctrl["armed"]:
                why = "not armed"
            elif self.halted:
                why = f"halted: {self.halted}"
            elif self.ctrl["lots"] <= 0:
                why = "lots = 0"
            elif now.time() >= LAST_ENTRY:
                why = "after 15:10"
            elif self._entry_is_stale(paper, now):
                why = "armed after this paper entry (not chasing)"
            if why:
                self.skipped.add(tid)
                if why != "not armed":
                    self.event("LIVE", f"{tid} not mirrored: {why}")
                self._persist()
                return
            self._sell(paper)

    def _entry_is_stale(self, paper, now) -> bool:
        """A paper entry older than ~20 s was taken before we were armed / running."""
        try:
            import pandas as pd
            return (pd.Timestamp(now) - pd.Timestamp(paper["entry_ts"])).total_seconds() > 20
        except Exception:
            return False

    # ---------- orders ----------
    def _resolve(self, contract):
        p = self.prepared.get(self._key(contract))
        if p:
            return p["inst"]
        expiry, strike, opt = contract
        return self.ke.resolve(self.client, "NIFTY", expiry, int(strike), opt)

    def _price_retry(self, inst, side, qty, ref_price):
        """Re-price from Kotak's own depth (timed); fall back to the mark-based cap."""
        fallback = self.ke.marketable_limit(ref_price, side)
        try:
            return self._timed(CALL_TIMEOUT_S, self.ke.marketable_price, self.client, inst.get("token"),
                               inst["exchange_segment"], side, qty, fallback=fallback)
        except Exception:
            return fallback

    def _fill(self, inst, side, qty, ref_price, attempts=4, contract=None, t_decision=None):
        """Send `qty` as marketable limits (sliced at the freeze qty) and wait for fills.
        Attempt 1 is priced off our own live tick (no broker call before sending);
        retries re-price from Kotak's depth. Returns (filled_qty, avg_price, last_error)."""
        filled, value, err = 0, 0.0, None
        remaining = qty
        t_decision = t_decision or _ms()
        for attempt in range(1, attempts + 1):
            if remaining <= 0:
                break
            for chunk in self._slices(remaining):
                src = None
                if attempt == 1:
                    px, src = self._first_price(contract, side, inst)
                    if px is None:
                        px = self._price_retry(inst, side, chunk, ref_price)
                        src = f"depth ({src})"
                else:
                    px = self._price_retry(inst, side, chunk, ref_price)
                    src = "depth"
                t_sent = _ms()
                try:
                    oid = self.ke.place_limit(self.client, inst["trading_symbol"], inst["exchange_segment"],
                                              side, chunk, px, tag=TAG)
                except Exception as ex:
                    err = str(ex)
                    if side.upper().startswith("S") and self.ke.is_margin_error(ex):
                        return filled, (value / filled if filled else None), "MARGIN: " + err
                    self.event("WARN", f"{side} {chunk} {inst['trading_symbol']} @ {px} rejected: {err}")
                    continue
                t_ack = _ms()
                st = self._wait(oid, self.wait_first if attempt == 1 else self.wait_retry)
                t_seen = _ms()
                got = int(st.get("filled_qty") or 0)
                if got < chunk and not self._terminal(st):
                    try:
                        self._timed(CALL_TIMEOUT_S, self.ke.cancel, self.client, oid)
                    except Exception:
                        pass
                    st = self._wait(oid, self.confirm_s)           # a fill can land while cancelling
                    got = int(st.get("filled_qty") or 0)
                    t_seen = _ms()
                    if not self._terminal(st):
                        # state unknown: NEVER send another order on top of it
                        self.unsettled[oid] = {**self._ctx, "side": side, "symbol": inst["trading_symbol"],
                                               "qty": chunk, "counted": got, "since": self.clock().strftime("%H:%M:%S")}
                        err = f"UNSETTLED order {oid}: Kotak did not confirm cancel/fill — no new order until it does"
                        self.event("ERROR", err)
                        if got:
                            self._record_fill(side, inst, got, st, px, oid, attempt, src, t_decision, t_sent, t_ack, t_seen)
                            filled += got
                            value += got * float(st.get("avg_price") or px)
                        return filled, (value / filled if filled else None), err
                if got:
                    avg = float(st.get("avg_price") or px)
                    filled += got
                    value += got * avg
                    remaining -= got
                    self._record_fill(side, inst, got, st, px, oid, attempt, src, t_decision, t_sent, t_ack, t_seen)
                else:
                    err = f"order {oid} not filled at {px} ({src})"
                    self.event("WARN", f"{side} {chunk} {inst['trading_symbol']} @ {px} not filled in time "
                                       f"— cancelled, re-pricing from depth")
            if remaining > 0:
                self.sleep(0.2)
        return filled, (value / filled if filled else None), (err if remaining > 0 else None)

    def _record_fill(self, side, inst, got, st, px, oid, attempt, src, t_decision, t_sent, t_ack, t_seen):
        avg = float(st.get("avg_price") or px)
        f = {"time": self.clock().strftime("%H:%M:%S"), "side": side, "symbol": inst["trading_symbol"],
             "qty": got, "price": avg, "limit": px, "price_src": src, "order_id": oid, "attempt": attempt,
             "exch_time": st.get("fill_time"),
             "ms_decision_to_send": t_sent - t_decision, "ms_ack": t_ack - t_sent,
             "ms_send_to_fill_seen": t_seen - t_sent, "ms_total": t_seen - t_decision, **self._ctx}
        self.fills.append(f)
        self.realized = realized_fifo(self.fills)
        self.event("FILL", f"{side} {got} {inst['trading_symbol']} @ {avg:.2f} (order {oid}) — "
                           f"sent {f['ms_decision_to_send']} ms after decision, ack {f['ms_ack']} ms, "
                           f"fill seen {f['ms_send_to_fill_seen']} ms after send ({src})")

    @staticmethod
    def _slices(q):
        n = math.ceil(q / FREEZE_QTY)
        base = [FREEZE_QTY] * (n - 1)
        return base + [q - FREEZE_QTY * (n - 1)]

    @staticmethod
    def _terminal(st) -> bool:
        s = str(st.get("status") or "").lower()
        return (("complete" in s and "incomplete" not in s) or "traded" in s or s == "filled"
                or "reject" in s or ("cancel" in s and "pending" not in s))

    def _wait(self, oid, seconds):
        """Poll the order until terminal or the deadline. Each read has its own timeout, so
        a slow Kotak call can never stretch the wait (26-Oct: 12 s became 66 s)."""
        end = time.monotonic() + seconds
        st = {}
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return st
            try:
                got = self._timed(min(CALL_TIMEOUT_S, max(0.2, left)), self.ke.order_status, self.client, oid)
                if got and got.get("status") is not None:
                    st = got
            except FTimeout:
                pass
            except Exception:
                pass
            if self._terminal(st):
                return st
            self.sleep(0.15)

    def _settle(self):
        """Resolve orders whose state was unknown. Until every one is settled no new order
        is sent (an unseen fill must never be doubled)."""
        for oid, u in list(self.unsettled.items()):
            try:
                st = self._timed(CALL_TIMEOUT_S, self.ke.order_status, self.client, oid)
            except Exception:
                continue
            if not self._terminal(st):
                if not str(st.get("status") or "").lower().startswith("cancel"):
                    try:
                        self._timed(CALL_TIMEOUT_S, self.ke.cancel, self.client, oid)
                    except Exception:
                        pass
                continue
            got = int(st.get("filled_qty") or 0)
            extra = got - int(u.get("counted") or 0)
            del self.unsettled[oid]
            if extra > 0:
                avg = float(st.get("avg_price") or 0)
                self.fills.append({"time": self.clock().strftime("%H:%M:%S"), "side": u["side"],
                                   "symbol": u["symbol"], "qty": extra, "price": avg, "order_id": oid,
                                   "attempt": "settled", "trade_id": u.get("trade_id"),
                                   "leg": u.get("leg"), "reason": u.get("reason")})
                self.realized = realized_fifo(self.fills)
                sgn = -1 if u["side"].upper().startswith("S") else 1
                if self.pos and self.pos.get("symbol") == u["symbol"]:
                    self.pos["qty"] = int(self.pos["qty"]) - sgn * extra
                    if self.pos["qty"] <= 0:
                        self.pos = None
                        self.pending_exit = None
                elif sgn < 0:
                    self.pos = {"symbol": u["symbol"], "qty": extra, "avg": avg, "seg": "nse_fo"}
                self.event("LIVE", f"settled order {oid}: +{extra} {u['side']} filled late @ {avg}")
            else:
                self.event("LIVE", f"settled order {oid}: {st.get('status')} (no extra fill)")
            self._persist()

    def _sell(self, paper):
        tid = paper["trade_id"]
        self.mirrored.add(tid)                        # marked first: never sent twice
        self._persist()
        if self.ensure_client() is None:
            self.last_error = f"no Kotak D session: {self.login_error}"
            self.event("ERROR", f"{tid} NOT mirrored — {self.last_error}")
            self._persist()
            return
        try:
            inst = self._resolve(paper["contract"])
        except Exception as ex:
            self.last_error = f"resolve failed: {ex}"
            self.event("ERROR", f"{tid} NOT mirrored — {self.last_error}")
            self._persist()
            return
        lot = int(inst.get("lot_size") or 0) or self.e.lot_size
        qty = self.ctrl["lots"] * lot
        self.event("LIVE", f"SELL {qty} ({self.ctrl['lots']} lots) {inst['trading_symbol']} "
                           f"— mirroring paper {tid} @ {paper['entry_price']}")
        self._ctx = {"trade_id": tid, "leg": "entry", "reason": "entry"}
        got, avg, err = self._fill(inst, "SELL", qty, paper["entry_price"], contract=paper["contract"],
                                   t_decision=getattr(self.e, "last_decision_ms", None))
        if got:
            self.pos = {"symbol": inst["trading_symbol"], "seg": inst["exchange_segment"],
                        "token": inst.get("token"), "qty": got, "avg": avg, "trade_id": tid,
                        "contract": [str(paper["contract"][0])[:10], int(paper["contract"][1]), paper["contract"][2]],
                        "lots": self.ctrl["lots"], "lot_size": lot,
                        "entry_time": self.clock().strftime("%H:%M:%S")}
        if err:
            self.last_error = err
            if err.startswith("MARGIN"):
                self.halted = "margin rejection"
            self.event("ERROR" if not got else "WARN",
                       f"{tid} sell incomplete: {got}/{qty} filled — {err}")
        self._persist()

    def _buy_back(self, reason):
        if not self.pos:
            self.pending_exit = None
            return
        if self.ensure_client() is None:
            self.pending_exit = {"reason": reason, "since": self.clock().strftime("%H:%M:%S")}
            self.last_error = f"BUY-BACK PENDING, no Kotak D session: {self.login_error}"
            self._persist()
            return
        inst = {"trading_symbol": self.pos["symbol"], "exchange_segment": self.pos.get("seg", "nse_fo"),
                "token": self.pos.get("token")}
        ref = self.e.price_cached(self.e.contract_key(tuple(self.pos["contract"]))) if self.pos.get("contract") else None
        want = int(self.pos["qty"])
        self.event("LIVE", f"BUY {want} {inst['trading_symbol']} — {reason}")
        self._ctx = {"trade_id": self.pos.get("trade_id"), "leg": "exit", "reason": reason}
        got, avg, err = self._fill(inst, "BUY", want, ref or self.pos.get("avg") or 0,
                                   contract=self.pos.get("contract"),
                                   t_decision=getattr(self.e, "last_decision_ms", None))
        left = want - got
        if left <= 0:
            self.closed.add(self.pos.get("trade_id"))
            self.event("LIVE", f"Kotak D flat ({reason}); sold {self.pos.get('avg')} bought {avg}")
            self.pos = None
            self.pending_exit = None
        else:
            self.pos["qty"] = left
            self.pending_exit = {"reason": reason, "since": self.clock().strftime("%H:%M:%S")}
            self.last_error = f"BUY-BACK INCOMPLETE: {left} still short — retrying ({err})"
            self.event("ERROR", self.last_error)
        self._persist()

    def kill(self, why):
        self.halted = f"killed ({why})"
        if self.pos:
            self._buy_back(f"KILL: {why}")
        write_control(armed=False, kill=False)
        self.ctrl = read_control()
        self.event("LIVE", f"Kotak D KILLED — disarmed, no more entries today ({why})")
        self._persist()

    # ---------- view ----------
    def view(self):
        ltp = None
        if self.pos and self.pos.get("contract"):
            ltp = self.e.price_cached(self.e.contract_key(tuple(self.pos["contract"])))
        realized = self.realized
        open_pnl = (self.pos["avg"] - ltp) * self.pos["qty"] if (self.pos and ltp and self.pos.get("avg")) else 0.0
        trips = round_trips(self.fills, self.date, {self.pos["symbol"]: ltp} if (self.pos and ltp) else None)
        link_paper(trips, list(self.e.machine.trades))
        return {"trades": trips, "pos_detail": self.pos_detail(ltp), "mtm": self.mtm(ltp),
                "armed": self.ctrl["armed"], "lots": self.ctrl["lots"], "halted": self.halted,
                "logged_in": self.client is not None, "login_error": self.login_error,
                "position": dict(self.pos, ltp=ltp) if self.pos else None,
                "pending_exit": self.pending_exit, "last_error": self.last_error,
                "fills": self.fills[-30:], "realized_gross": round(realized, 2),
                "unsettled": self.unsettled,
                "prepared": sorted(v["inst"].get("trading_symbol") for v in self.prepared.values()),
                "open_gross": round(open_pnl, 2), "day_gross": round(realized + open_pnl, 2)}

    def pos_detail(self, ltp=None):
        """The open Kotak D short with what the page shows: sold price/time, LTP, expiry,
        the strategy's stop to cost (it follows the paper position: arms when the option
        has traded `x` points below the paper sell, then buys back at the paper cost)."""
        pos = self.pos
        if not pos:
            return None
        if ltp is None and pos.get("contract"):
            ltp = self.e.price_cached(self.e.contract_key(tuple(pos["contract"])))
        x = (getattr(self.e, "p", {}) or {}).get("breakeven_after_points")
        paper = self.e.machine.position
        same = paper is not None and paper.get("trade_id") == pos.get("trade_id")
        stop = None
        if x and same:
            pe = float(paper["entry_price"])
            armed = paper.get("be_armed_ts")
            stop = {"price": round(pe, 2), "trigger": round(pe - x, 2), "points": x,
                    "armed": armed.strftime("%H:%M:%S") if hasattr(armed, "strftime") else armed}
        avg = pos.get("avg")
        gross = (avg - ltp) * pos["qty"] if (avg and ltp) else None
        c = pos.get("contract") or [None, None, None]
        return {"symbol": pos["symbol"], "qty": pos["qty"], "lots": pos.get("lots"), "avg": avg,
                "entry_time": pos.get("entry_time"), "ltp": ltp, "expiry": c[0],
                "strike": c[1], "opt": c[2], "trade_id": pos.get("trade_id"),
                "gross": round(gross, 2) if gross is not None else None, "stop": stop}

    def mtm(self, ltp=None):
        """Small per-tick snapshot for the live stream: position, LTP, gross + est. net."""
        pos = self.pos
        if pos and ltp is None and pos.get("contract"):
            ltp = self.e.price_cached(self.e.contract_key(tuple(pos["contract"])))
        open_g = (pos["avg"] - ltp) * pos["qty"] if (pos and ltp and pos.get("avg")) else 0.0
        day_g = self.realized + open_g
        # charges: ~₹ per round trip on NIFTY 1 lot is small; estimated from statutory_costs
        est = 0.0
        try:
            from backtesting.options_pivot_intraday.costs import statutory_costs
            by = {}
            for f in self.fills:
                by.setdefault(f["symbol"], {"S": [0, 0.0], "B": [0, 0.0]})
                k = "S" if str(f["side"]).upper().startswith("S") else "B"
                by[f["symbol"]][k][0] += int(f["qty"])
                by[f["symbol"]][k][1] += int(f["qty"]) * float(f["price"])
            for v in by.values():
                q = max(v["S"][0], v["B"][0])
                if not q:
                    continue
                sp = v["S"][1] / v["S"][0] if v["S"][0] else 0.0
                bp = v["B"][1] / v["B"][0] if v["B"][0] else (ltp or 0.0)
                est += statutory_costs(sp, bp, q, self.date)["total"]
        except Exception:
            pass
        return {"pos": self.pos_detail(ltp) if pos else None,
                "realized": round(self.realized, 2), "open": round(open_g, 2),
                "gross": round(day_g, 2), "net": round(day_g - est, 2), "armed": self.ctrl.get("armed")}
