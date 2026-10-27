"""
mcx_terminal/live.py — the LIVE order book: real orders on Kotak P (env keys KOTAK_*_D).
======================================================================================

Same screen interface as the paper book (place / cancel / exit / basket / move / positions), but
every slice goes to Kotak as a LIMIT order and fills come from Kotak's order book, never invented.

Facts this file relies on (checked on the live API, 27-Oct-2026, read-only):
  * MCX quantity is sent in UNITS: qty=1 is refused ("please provide valid lotwise quantity"),
    qty=100 is 1 lot of CRUDEOIL. The engine already works in units (lots x lot size).
  * Kotak takes no MARKET order through the API here -> "MKT" = the same system-priced limit as paper.
  * A repeated order tag is refused as a duplicate client id -> every order gets its own tag
    (prefix TAG, so after a restart our orders are found again in Kotak's order book).

Own book only: positions are built from THIS terminal's own fills (tag prefix TAG). Manual trades on
the same Kotak P account (the owner trades MCX there by hand too) are never counted, netted or closed.

Safety:
  * new risk only while the owner has ARMED live (armed() is True); exits of positions this book
    already holds are always allowed, so the MTM guard / EXIT ALL still work after a disarm
  * a read failure is never taken as "no fill": the order stays OPEN until Kotak says otherwise
"""
import datetime as dt
import logging
import threading
import time

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.paper import IST, PaperBook, _round_tick, today

log = logging.getLogger("mcx.live")

SEG = "mcx_fo"
TAG = "mcxd"                       # order tag prefix: MCX Desk orders on Kotak P
FAST_POLL_S = 0.4                  # order-book poll while an order is working
SLOW_POLL_S = 10.0                 # otherwise (finds orders a crash left behind)
VERDICT_WAIT_S = 1.5               # a MKT place() waits up to this for Kotak's verdict (pop-up colour)
TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED", "PART-CANCELLED")

_OID_KEYS = ("nOrdNo", "orderId", "order_id", "ordNo")


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _dig(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def extract_oid(resp):
    if not isinstance(resp, dict):
        return None
    oid = _dig(resp, *_OID_KEYS)
    if oid:
        return str(oid)
    data = resp.get("data")
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        oid = _dig(data, *_OID_KEYS)
        return str(oid) if oid else None
    return None


def resp_error(resp):
    """Readable reason if a Kotak response is a failure, else None."""
    if not isinstance(resp, dict):
        return f"unexpected reply: {str(resp)[:160]}"
    for k in ("Error", "error", "Error Message"):
        if resp.get(k):
            return str(resp[k])[:200]
    if extract_oid(resp):
        return None
    d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    stat = str(d.get("stat", "")).strip().lower()
    if d.get("errMsg") or (stat and stat not in ("ok", "success")):
        return str(d.get("errMsg") or d.get("stat") or f"stCode={d.get('stCode')}")[:200]
    return None


class LiveBook(PaperBook):
    PREFIX = "live_book_"

    def __init__(self, universe, quotes, sessions, armed, on_change=None, path=None, settings=None,
                 client=None, start_poller=True):
        """sessions: SessionHolder (Kotak P). armed(): True while the owner has armed live.
        client: test hook (a fake Kotak client); normally sessions.get().client."""
        self.sessions = sessions
        self.armed = armed
        self._client_override = client
        self.by_symbol = {c.symbol: c for c in universe.by_token.values()}
        self._seq = 0
        self._poll_kick = threading.Event()
        self.broker = {"ok": True, "msg": "", "at": None}
        super().__init__(universe, quotes, on_change=on_change, path=path or (C.DATA / f"live_book_{today()}.json"),
                         settings=settings)
        if start_poller:
            threading.Thread(target=self._poll_loop, daemon=True, name="LiveOrders").start()

    # ── broker plumbing ─────────────────────────────────────────────────────
    def _client(self):
        return self._client_override or self.sessions.get().client

    def _call(self, fn_name, **kw):
        """One Kotak REST call; on a session-looking failure log in again once and retry."""
        try:
            r = getattr(self._client(), fn_name)(**kw)
        except Exception as e:
            r = {"Error": f"{type(e).__name__}: {e}"}
        txt = str(r)[:300].lower() if not isinstance(r, list) else ""
        if self._client_override is None and any(w in txt for w in ("2fa", "session", "unauthor", "invalid token", "401")):
            log.warning("Kotak %s looked like an expired session (%s) - logging in again", fn_name, txt[:120])
            try:
                self.sessions.get(force=True)
                r = getattr(self._client(), fn_name)(**kw)
            except Exception as e:
                r = {"Error": f"{type(e).__name__}: {e}"}
        return r

    def _tag(self) -> str:
        self._seq = (self._seq + 1) % 1000
        return f"{TAG}{dt.datetime.now(IST):%H%M%S}{self._seq:03d}"

    @staticmethod
    def _px(p: float) -> str:
        return f"{p:.2f}"

    # ── orders ──────────────────────────────────────────────────────────────
    def place(self, token: int, side: str, qty: int, kind: str = "MKT", limit: float | None = None,
              source: str = "", buffer_pct: float | None = None) -> dict:
        pr = self._prepare(token, side, qty, kind, limit, buffer_pct)
        if "c" not in pr:
            return pr
        c, side, lots, st, role, limit, slices = (pr[k] for k in ("c", "side", "lots", "st", "role", "limit", "slices"))
        if not self.armed():
            with self.lock:
                p = self.pos.get(token)
                reduces = bool(p and p["net"] and (p["net"] > 0) == (side == "S") and qty <= abs(p["net"]))
            if not reduces:
                return {"ok": False, "msg": "LIVE is not armed - only exits of open live positions are allowed"}
        product = st.get("product", "NRML") if st.get("product") in ("NRML", "MIS") else "NRML"
        if role == "exit":                      # close in the SAME product, or Kotak opens a new opposite position
            with self.lock:
                prev = next((o["product"] for o in reversed(self.orders)
                             if o["token"] == token and o["filled"] and o.get("product") in ("NRML", "MIS")), None)
            product = prev or product
        ts = dt.datetime.now(IST).strftime("%H:%M:%S.%f")[:-3]
        mine = []
        for i, sq in enumerate(slices):
            o = {"id": next(self._ids), "ts": ts, "t0": time.time(), "token": token, "symbol": c.symbol,
                 "name": c.name, "side": side, "qty": sq, "lot": c.lot, "kind": kind, "limit": limit,
                 "filled": 0, "avg": 0.0, "status": "SENDING", "reason": "", "source": source, "role": role,
                 "product": product, "session": st.get("session", "AUTO"), "t_rep": time.time(),
                 "slice": f"{i + 1}/{len(slices)}" if len(slices) > 1 else "", "live": True,
                 "tag": self._tag(), "broker_id": None}
            with self.lock:
                self.orders.append(o)
                self._save()                                   # on disk BEFORE it goes to Kotak
            t = time.time()
            r = self._call("place_order", exchange_segment=SEG, product=product, price=self._px(limit),
                           order_type="L", quantity=str(int(sq)), validity="DAY", trading_symbol=c.symbol,
                           transaction_type=side, tag=o["tag"])
            ms = (time.time() - t) * 1000
            err, oid = resp_error(r), extract_oid(r)
            with self.lock:
                if oid and not err:
                    o.update(broker_id=oid, status="OPEN", reason=f"sent to Kotak ({ms:.0f} ms)")
                else:
                    o.update(status="REJECTED", reason=f"Kotak refused: {err or 'no order id'}")
                self._save()
            log.warning("LIVE %s %s %d (%s) limit %s tag %s -> %s %s (%.0f ms)", side, c.symbol, sq,
                        o["slice"] or "1/1", limit, o["tag"], oid, err or "", ms)
            mine.append(o)
        self.on_change()
        self._poll_kick.set()
        deadline = time.time() + (VERDICT_WAIT_S if kind == "MKT" else 0.4)   # wait briefly for Kotak's verdict
        while time.time() < deadline and any(o["status"] not in TERMINAL for o in mine if o["broker_id"]):
            time.sleep(0.25)
            self.poll()
        return self._result(mine, side, lots, c, qty, limit)

    @staticmethod
    def _result(mine, side, lots, c, qty, limit) -> dict:
        ids = [o["id"] for o in mine]
        filled = sum(o["filled"] for o in mine)
        avg = sum(o["avg"] * o["filled"] for o in mine) / filled if filled else None
        rej = [o for o in mine if o["status"] == "REJECTED"]
        word = "BOUGHT" if side == "B" else "SOLD"
        s_ = "s" if lots != 1 else ""
        if rej and not filled and len(rej) == len(mine):
            return {"ok": False, "ids": ids, "state": "rejected",
                    "msg": f"REJECTED {lots} lot{s_} {c.symbol}: {rej[0]['reason']}"}
        if filled >= qty:
            state, msg = "filled", f"{word} {lots} lot{s_} {c.symbol} @ {avg:,.2f} (Kotak P LIVE)"
        elif filled:
            state, msg = "partial", (f"{word} {filled // c.lot} of {lots} lots {c.symbol} @ {avg:,.2f} · "
                                     f"rest working at {limit:g} (LIVE)")
        else:
            state, msg = "waiting", f"{'BUY' if side == 'B' else 'SELL'} {lots} lot{s_} {c.symbol} working at {limit:g} (LIVE)"
        if rej:
            msg += f" · {len(rej)} slice(s) REJECTED: {rej[0]['reason']}"
        return {"ok": True, "ids": ids, "state": state, "msg": msg}

    def on_tick(self, token):
        """Live fills come from Kotak, not from our view of the book."""
        return

    # ── Kotak order book -> our orders ──────────────────────────────────────
    def _rows(self):
        r = self._call("order_report")
        if isinstance(r, dict):
            d = r.get("data")
            if isinstance(d, list):
                return d
            if r.get("Error") or r.get("error"):
                raise RuntimeError(str(r.get("Error") or r.get("error"))[:200])
            return []                                     # {'stat': 'Not_Ok', 'errMsg': 'No Data'} = no orders
        return r if isinstance(r, list) else []

    def poll(self) -> bool:
        """Read Kotak's order book once and apply new fills / status changes. -> changed?"""
        try:
            rows = self._rows()
        except Exception as e:
            self.broker = {"ok": False, "msg": f"cannot read Kotak orders: {e}", "at": time.time()}
            return False
        self.broker = {"ok": True, "msg": "", "at": time.time()}
        changed = False
        with self.lock:
            by_id = {str(_dig(r, *_OID_KEYS)): r for r in rows if _dig(r, *_OID_KEYS)}
            known = {o["broker_id"] for o in self.orders if o.get("broker_id")}
            known_tags = {o.get("tag") for o in self.orders}
            for oid, r in by_id.items():
                if oid in known:
                    continue
                tg = str(_dig(r, "tag", "orderTag", "ig") or "")
                if not tg.startswith(TAG):
                    continue                               # not ours (manual trades on the same account)
                lost = next((o for o in self.orders if o.get("tag") == tg and not o.get("broker_id")), None)
                if lost is not None:                       # sent, but Kotak's reply never reached us
                    lost.update(broker_id=oid, status="OPEN", reason="Kotak id found in its order book")
                    changed = True
                    continue
                if tg in known_tags:
                    continue
                # ours by tag but missing from the book file entirely (a crash): adopt it
                c = self.by_symbol.get(str(r.get("trdSym", "")))
                if c is None:
                    log.warning("Kotak order %s tag %s for unknown symbol %s - not adopted", oid, tg, r.get("trdSym"))
                    continue
                o = {"id": next(self._ids), "ts": str(r.get("ordDtTm", ""))[-8:], "t0": time.time(), "token": c.token,
                     "symbol": c.symbol, "name": c.name, "side": str(r.get("trnsTp", "B")).upper()[:1],
                     "qty": int(_f(r.get("qty")) or 0), "lot": c.lot, "kind": "LMT", "limit": _f(r.get("prc")) or 0.0,
                     "filled": 0, "avg": 0.0, "status": "OPEN", "reason": "found in Kotak's order book after a restart",
                     "source": "recovered", "role": "entry", "product": r.get("prod", "NRML"), "session": "AUTO",
                     "t_rep": time.time(), "slice": "", "live": True, "tag": tg, "broker_id": oid}
                self.orders.append(o)
                log.warning("adopted Kotak order %s (tag %s) %s %s %s", oid, tg, o["side"], c.symbol, o["qty"])
                changed = True
            for o in self.orders:
                if o["status"] == "SENDING" and not o.get("broker_id") and time.time() - o.get("t0", 0) > 60:
                    o.update(status="REJECTED", reason="no reply from Kotak and not in its order book")
                    changed = True
                if o["status"] in TERMINAL or not o.get("broker_id"):
                    continue
                r = by_id.get(o["broker_id"])
                if r is None:
                    continue
                changed |= self._apply_row(o, r)
            if changed:
                self._save()
        if changed:
            self.on_change()
        return changed

    def _apply_row(self, o, r) -> bool:
        st = str(r.get("ordSt", "")).strip().lower()
        fld = int(_f(_dig(r, "fldQty", "filledQty")) or 0)
        avg = _f(r.get("avgPrc")) or 0.0
        changed = False
        if fld > o["filled"]:
            d = fld - o["filled"]
            px = (avg * fld - o["avg"] * o["filled"]) / d if avg else (o["limit"] or 0.0)
            self._fill(o, round(px, 4), d)
            if avg:
                o["avg"] = avg
            changed = True
        prc = _f(r.get("prc"))
        if prc and abs(prc - (o["limit"] or 0)) > 1e-9:
            o["limit"] = prc
            changed = True
        if st != o.get("broker_status"):
            o["broker_status"] = st
            changed = True
        new = o["status"]
        if fld >= o["qty"] or st in ("complete", "traded"):
            new = "COMPLETE"
        elif "reject" in st:
            new = "REJECTED"
            o["reason"] = f"Kotak rejected: {r.get('rejRsn') or st}"[:240]
        elif "cancel" in st:
            new = "PART-CANCELLED" if o["filled"] else "CANCELLED"
        if new != o["status"]:
            o["status"] = new
            if new == "COMPLETE":
                o["reason"] = f"filled by Kotak @ {o['avg']:,.2f}"
            changed = True
            log.warning("LIVE order %s %s %s %s -> %s (filled %d @ %s)", o["broker_id"], o["side"], o["symbol"],
                        o["qty"], new, o["filled"], o["avg"])
        return changed

    def _poll_loop(self):
        while True:
            try:
                with self.lock:
                    working = any(o["status"] not in TERMINAL for o in self.orders)
                self._poll_kick.wait(FAST_POLL_S if working else SLOW_POLL_S)
                self._poll_kick.clear()
                self.poll()
            except Exception:
                log.exception("live poll")
                time.sleep(1)

    # ── working orders: re-price / push / cancel ────────────────────────────
    def _modify(self, o, price) -> str | None:
        c = self.u.by_token[o["token"]]
        r = self._call("modify_order", order_id=str(o["broker_id"]), price=self._px(price), order_type="L",
                       quantity=str(int(o["qty"])), validity="DAY", instrument_token=str(c.token),
                       exchange_segment=SEG, product=o["product"], trading_symbol=c.symbol,
                       transaction_type=o["side"], filled_quantity=str(int(o["filled"])))
        err = resp_error(r)
        if err:
            log.warning("modify %s to %s refused: %s", o["broker_id"], price, err)
        return err

    def housekeep(self):
        """Re-price working system orders each second (if set) and push old ones through the book."""
        st = self.settings()
        now = time.time()
        with self.lock:
            work = [o for o in self.orders if o["status"] == "OPEN" and o.get("broker_id")]
        mkt_after = float(st.get("market_after_s") or 0)
        for o in work:
            if now - o.get("t_rep", 0) < 1.0:
                continue
            o["t_rep"] = now
            if mkt_after and now - o.get("t0", now) >= mkt_after and not o.get("pushed"):
                lim = self._aggressive(o["token"], o["side"])
                if lim and not self._modify(o, lim):
                    o["pushed"] = True
                    o["reason"] += f"; pushed to {lim:g} after {mkt_after:g}s"
                continue
            if o["kind"] == "MKT" and st.get("reprice"):
                lim = self._price(o["token"], o["side"], o.get("role", "entry"), st)
                if lim and abs(lim - (o["limit"] or 0)) > 1e-9 and not self._modify(o, lim):
                    o["reprices"] = o.get("reprices", 0) + 1
        if work:
            self._poll_kick.set()

    def cancel(self, oid: int) -> dict:
        with self.lock:
            o = next((x for x in self.orders if x["id"] == oid and x["status"] not in TERMINAL), None)
        if o is None:
            return {"ok": False, "msg": "no working order with that id"}
        if not o.get("broker_id"):
            return {"ok": False, "msg": "order has no Kotak id yet - try again in a moment"}
        err = resp_error(self._call("cancel_order", order_id=str(o["broker_id"])))
        self._poll_kick.set()
        if err:
            return {"ok": False, "msg": f"Kotak refused the cancel: {err}"}
        return {"ok": True, "msg": f"cancel sent for #{oid} (Kotak {o['broker_id']})"}

    def cancel_all(self) -> int:
        with self.lock:
            ids = [o["id"] for o in self.orders if o["status"] not in TERMINAL and o.get("broker_id")]
        return sum(1 for i in ids if self.cancel(i)["ok"])

    def exit_all(self, fraction: float = 1.0) -> dict:
        """Cancel working orders first and let Kotak confirm, so an open order cannot fill after the exit."""
        n_cancel = self.cancel_all()
        if n_cancel:
            end = time.time() + 1.5
            while time.time() < end:
                self.poll()
                with self.lock:
                    if not any(o["status"] not in TERMINAL for o in self.orders):
                        break
                time.sleep(0.2)
        res = [self.exit_position(t, fraction, "exit all") for t, p in list(self.pos.items()) if p["net"]]
        bad = [r["msg"] for r in res if not r["ok"]]
        states = {r.get("state") for r in res if r["ok"]}
        state = "filled" if states <= {"filled"} else "waiting"
        return {"ok": not bad, "state": state if res else "filled",
                "msg": (f"EXITED {len(res)} live position(s)" if state == "filled" else
                        f"exit sent for {len(res)} live position(s), some still working")
                + (f", {n_cancel} order(s) cancelled" if n_cancel else "") + (f" · PROBLEM: {'; '.join(bad)}" if bad else "")}

    # ── views ───────────────────────────────────────────────────────────────
    def tokens(self) -> set:
        with self.lock:
            return {t for t, p in self.pos.items() if p["net"]} | {o["token"] for o in self.orders if o["status"] not in TERMINAL}

    def working(self) -> int:
        with self.lock:
            return sum(1 for o in self.orders if o["status"] not in TERMINAL)

    def open_positions(self) -> int:
        with self.lock:
            return sum(1 for p in self.pos.values() if p["net"])
