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
"""
import json
import math
import time
import datetime as dt
import importlib.util
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

    # ---------- plumbing ----------
    def event(self, kind, msg):
        self.e.event(kind, f"[KOTAK D] {msg}")

    def ensure_client(self):
        if self.client is not None:
            return self.client
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
                "skipped": sorted(self.skipped)}, default=str), encoding="utf-8")
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
        expiry, strike, opt = contract
        return self.ke.resolve(self.client, "NIFTY", expiry, int(strike), opt)

    def _fill(self, inst, side, qty, ref_price, attempts=4, wait_s=12.0):
        """Send `qty` as marketable limits (sliced at the freeze qty) and wait for fills.
        Returns (filled_qty, avg_price, last_error)."""
        filled, value, err = 0, 0.0, None
        remaining = qty
        for attempt in range(1, attempts + 1):
            if remaining <= 0:
                break
            for chunk in self._slices(remaining):
                px = self.ke.marketable_price(self.client, inst.get("token"), inst["exchange_segment"],
                                              side, chunk,
                                              fallback=self.ke.marketable_limit(ref_price, side))
                try:
                    oid = self.ke.place_limit(self.client, inst["trading_symbol"], inst["exchange_segment"],
                                              side, chunk, px, tag=TAG)
                except Exception as ex:
                    err = str(ex)
                    if side.upper().startswith("S") and self.ke.is_margin_error(ex):
                        return filled, (value / filled if filled else None), "MARGIN: " + err
                    self.event("WARN", f"{side} {chunk} {inst['trading_symbol']} @ {px} rejected: {err}")
                    continue
                st = self._wait(oid, wait_s)
                got = int(st.get("filled_qty") or 0)
                if got < chunk:
                    self.ke.cancel(self.client, oid)
                    st = self._wait(oid, 3.0)                 # a fill can land while cancelling
                    got = int(st.get("filled_qty") or 0)
                if got:
                    avg = float(st.get("avg_price") or px)
                    filled += got
                    value += got * avg
                    remaining -= got
                    self.fills.append({"time": self.clock().strftime("%H:%M:%S"), "side": side,
                                       "symbol": inst["trading_symbol"], "qty": got, "price": avg,
                                       "order_id": oid, "attempt": attempt})
                    self.event("FILL", f"{side} {got} {inst['trading_symbol']} @ {avg:.2f} (order {oid})")
                else:
                    err = f"order {oid} not filled at {px}"
            if remaining > 0:
                self.sleep(1.0)
        return filled, (value / filled if filled else None), (err if remaining > 0 else None)

    @staticmethod
    def _slices(q):
        n = math.ceil(q / FREEZE_QTY)
        base = [FREEZE_QTY] * (n - 1)
        return base + [q - FREEZE_QTY * (n - 1)]

    def _wait(self, oid, seconds):
        end = time.monotonic() + seconds
        st = {}
        while True:
            st = self.ke.order_status(self.client, oid)
            s = str(st.get("status") or "").lower()
            if ("complete" in s and "incomplete" not in s) or "traded" in s or s == "filled":
                return st
            if "reject" in s or ("cancel" in s and "pending" not in s):
                return st
            if time.monotonic() >= end:
                return st
            self.sleep(0.5)

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
        got, avg, err = self._fill(inst, "SELL", qty, paper["entry_price"])
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
        got, avg, err = self._fill(inst, "BUY", want, ref or self.pos.get("avg") or 0)
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
        realized = 0.0
        sells = [f for f in self.fills if str(f["side"]).upper().startswith("S")]
        buys = [f for f in self.fills if str(f["side"]).upper().startswith("B")]
        realized_qty = min(sum(f["qty"] for f in sells), sum(f["qty"] for f in buys))
        if realized_qty:
            s_avg = sum(f["qty"] * f["price"] for f in sells) / sum(f["qty"] for f in sells)
            b_avg = sum(f["qty"] * f["price"] for f in buys) / sum(f["qty"] for f in buys)
            realized = (s_avg - b_avg) * realized_qty
        open_pnl = (self.pos["avg"] - ltp) * self.pos["qty"] if (self.pos and ltp and self.pos.get("avg")) else 0.0
        return {"armed": self.ctrl["armed"], "lots": self.ctrl["lots"], "halted": self.halted,
                "logged_in": self.client is not None, "login_error": self.login_error,
                "position": dict(self.pos, ltp=ltp) if self.pos else None,
                "pending_exit": self.pending_exit, "last_error": self.last_error,
                "fills": self.fills[-30:], "realized_gross": round(realized, 2),
                "open_gross": round(open_pnl, 2), "day_gross": round(realized + open_pnl, 2)}
