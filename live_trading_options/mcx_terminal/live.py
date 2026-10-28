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

POSITIONS = A MIRROR OF KOTAK (owner's rule, 28-Oct-2026). Every POS_POLL_S seconds the book's positions
are replaced by Kotak P's own MCX positions — including trades placed by hand in Kotak, and positions
squared off there. Nothing is carried over from the terminal's memory (an own-book carried overnight
made EXIT ALL BUY back positions the owner had already closed by hand, 26/27-Oct).

EXIT ALL flattens the BROKER: fresh read of Kotak positions -> cancel every working MCX order on the
account -> all exits sent IN PARALLEL (closing shorts first) -> re-read Kotak until it shows flat,
pushing slow exits through the book. Sequential sending let one 60-second Kotak hang push every other
exit past the 23:30 close (27-Oct: all 12 rejected).

Safety:
  * new risk only while LIVE is ARMED; exits of open Kotak positions are always allowed
  * can't read Kotak = UNKNOWN, never flat: the screen is told, and EXIT ALL refuses to act on stale data
  * a read failure is never taken as "no fill": the order stays OPEN until Kotak says otherwise
"""
import datetime as dt
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fwait

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import fastrest
from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.paper import IST, PaperBook, _round_tick, today

log = logging.getLogger("mcx.live")

SEG = "mcx_fo"
TAG = "mcxd"                       # order tag prefix: MCX Desk orders on Kotak P
BURST_POLL_S = 0.15                # order-book poll for BURST_S right after an order is sent (fills show fast)
BURST_S = 3.0
FAST_POLL_S = 0.4                  # order-book poll while an order is working
SLOW_POLL_S = 10.0                 # otherwise (finds orders a crash left behind)
VERDICT_WAIT_S = 0.8               # a MKT place() waits up to this for Kotak's fill (pop-up colour); later fills are pushed
POS_POLL_S = 2.0                   # Kotak positions -> mirror
POS_STALE_S = 15.0                 # older than this = positions UNKNOWN on the screen
EXIT_PUSH_S = 2.5                  # EXIT ALL: an exit still working after this is pushed through the book
EXIT_VERIFY_S = 8.0                # EXIT ALL: re-read Kotak this long, waiting to see flat
TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED", "PART-CANCELLED")
BROKER_DONE = ("complete", "traded", "rejected", "cancelled")

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
        self._pos_kick = threading.Event()
        self._burst_until = 0.0
        self.notify = None                              # engine hook: a fill / reject that came after the pop-up
        self.broker = {"ok": True, "msg": "", "at": None}
        self.pos_sync = {"ok": False, "msg": "not read yet", "at": None}
        self._pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="KotakSend")
        super().__init__(universe, quotes, on_change=on_change, path=path or (C.DATA / f"live_book_{today()}.json"),
                         settings=settings)
        self.pos = {}                                   # positions come ONLY from Kotak (never from the file)
        if start_poller:
            self.sync_positions()
            threading.Thread(target=self._poll_loop, daemon=True, name="LiveOrders").start()
            threading.Thread(target=self._pos_loop, daemon=True, name="LivePositions").start()

    def _carry_forward(self):
        """Never: Kotak is the truth for positions, every day starts from what Kotak shows."""
        return

    def _apply(self, token, side, px, q):
        """A fill changes Kotak's positions; re-read them now instead of computing our own."""
        self._pos_kick.set()

    # ── Kotak positions -> mirror ───────────────────────────────────────────
    def _pos_rows(self) -> list:
        r = self._call("positions")
        if isinstance(r, list):
            return r
        if isinstance(r, dict):
            d = r.get("data")
            if isinstance(d, list):
                return d
            for k in ("Error", "error", "Error Message"):
                if r.get(k):
                    raise RuntimeError(str(r[k])[:200])
            msg = str(r.get("errMsg") or r.get("message") or r.get("stat") or "")
            if "no data" in msg.lower():
                return []                                   # Kotak's way of saying: no positions today
            raise RuntimeError(f"unexpected positions reply: {str(r)[:160]}")
        raise RuntimeError(f"unexpected positions reply: {str(r)[:160]}")

    def sync_positions(self, raise_errors: bool = False) -> bool:
        """Replace the book's positions with Kotak's MCX positions. -> changed?"""
        try:
            rows = self._pos_rows()
        except Exception as e:
            first = self.pos_sync.get("ok", True)
            self.pos_sync = {**self.pos_sync, "ok": False, "msg": f"cannot read Kotak positions: {e}"}
            if first:
                log.warning("positions read failed: %s", e)
                audit.write("broker", what="positions NOT readable", detail=str(e)[:200])
                self.on_change()
            if raise_errors:
                raise
            return False
        new, unknown = {}, []
        for r in rows:
            if "mcx" not in str(r.get("exSeg", "")).lower():
                continue
            n = lambda k: _f(r.get(k)) or 0.0
            bq, sq = n("cfBuyQty") + n("flBuyQty"), n("cfSellQty") + n("flSellQty")
            ba, sa = n("cfBuyAmt") + n("buyAmt"), n("cfSellAmt") + n("sellAmt")
            if not bq and not sq:
                continue
            c = None
            try:
                c = self.u.by_token.get(int(r.get("tok")))
            except (TypeError, ValueError):
                pass
            c = c or self.by_symbol.get(str(r.get("trdSym", "")))
            if c is None:
                unknown.append(str(r.get("trdSym")))
                continue
            p = new.setdefault(c.token, {"token": c.token, "symbol": c.symbol, "name": c.name, "lot": c.lot,
                                         "mult": c.mult, "net": 0, "avg": 0.0, "realized": 0.0, "bought": 0, "sold": 0,
                                         "_ba": 0.0, "_sa": 0.0, "by_prod": {}})
            p["bought"] += int(bq)
            p["sold"] += int(sq)
            p["_ba"] += ba
            p["_sa"] += sa
            prod = str(r.get("prod") or "NRML").upper()
            p["by_prod"][prod] = p["by_prod"].get(prod, 0) + int(bq - sq)
        for p in new.values():
            bq, sq = p["bought"], p["sold"]
            bavg = p["_ba"] / bq if bq else 0.0
            savg = p["_sa"] / sq if sq else 0.0
            p["net"] = bq - sq
            p["avg"] = round(bavg if p["net"] > 0 else savg if p["net"] < 0 else 0.0, 4)
            p["realized"] = round((savg - bavg) * min(bq, sq) * p["mult"], 2)
            del p["_ba"], p["_sa"]
        with self.lock:
            sig = lambda d: {t: (x["net"], round(x["avg"], 2), round(x["realized"], 2)) for t, x in d.items()}
            old = sig(self.pos)
            changed = old != sig(new)
            if changed:
                for t in set(old) | set(new):
                    a, b = old.get(t, (0, 0, 0))[0], sig(new).get(t, (0, 0, 0))[0]
                    if a != b:
                        sym = (new.get(t) or self.pos.get(t) or {}).get("symbol", t)
                        audit.write("broker", what=f"Kotak position {sym}: net {a} -> {b}")
            self.pos = new
            was = self.pos_sync.get("ok")
            self.pos_sync = {"ok": True, "msg": (f"unknown symbols at Kotak: {unknown}" if unknown else ""),
                             "at": time.time()}
            if changed:
                self._save()
        if not was:
            audit.write("broker", what="positions readable", open=sum(1 for p in new.values() if p["net"]))
        if changed or not was:
            self.on_change()
        return changed

    def positions_fresh(self) -> bool:
        a = self.pos_sync.get("at")
        return bool(self.pos_sync.get("ok") and a and time.time() - a <= POS_STALE_S)

    def _pos_loop(self):
        while True:
            try:
                self._pos_kick.wait(POS_POLL_S)
                self._pos_kick.clear()
                self.sync_positions()
            except Exception:
                log.exception("positions poll")
                time.sleep(1)

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
              source: str = "", buffer_pct: float | None = None, product: str | None = None) -> dict:
        pr = self._prepare(token, side, qty, kind, limit, buffer_pct)
        if "c" not in pr:
            return pr
        c, side, lots, st, role, limit, slices = (pr[k] for k in ("c", "side", "lots", "st", "role", "limit", "slices"))
        if not self.armed():
            with self.lock:
                p = self.pos.get(token)
                reduces = bool(p and p["net"] and (p["net"] > 0) == (side == "S") and qty <= abs(p["net"]))
            if not reduces:
                return {"ok": False, "msg": "LIVE is not armed - only exits of open Kotak positions are allowed"}
        if product not in ("NRML", "MIS"):
            product = st.get("product", "NRML") if st.get("product") in ("NRML", "MIS") else "NRML"
            if role == "exit":                  # close in the SAME product Kotak holds it in
                with self.lock:
                    bp = (self.pos.get(token) or {}).get("by_prod") or {}
                want = [pp for pp, n in bp.items() if n and (n > 0) == (side == "S") and pp in ("NRML", "MIS")]
                product = want[0] if want else product
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
                elif err and fastrest.is_timeout(err):
                    # no answer in time: the order MAY be at Kotak -> stay SENDING; the order-book poll
                    # finds it by its tag (or marks it rejected after 60 s if Kotak never had it)
                    o.update(reason=f"no reply from Kotak in {ms / 1000:.0f}s - checking its order book")
                else:
                    o.update(status="REJECTED", reason=f"Kotak refused: {err or 'no order id'}")
                self._save()
            log.warning("LIVE %s %s %d (%s) limit %s tag %s -> %s %s (%.0f ms)", side, c.symbol, sq,
                        o["slice"] or "1/1", limit, o["tag"], oid, err or "", ms)
            audit.write("order", what=f"LIVE {'BUY' if side == 'B' else 'SELL'} {sq // c.lot} lot(s) {c.symbol} "
                                      f"{product} limit {limit:g}", source=source, kotak_id=oid, ms=round(ms),
                        result=("sent" if oid and not err else f"REFUSED: {err or 'no order id'}"))
            mine.append(o)
        self.on_change()
        self._burst_until = time.time() + BURST_S
        self._poll_kick.set()
        deadline = time.time() + (VERDICT_WAIT_S if kind == "MKT" else 0.25)  # a short look for the fill
        while time.time() < deadline and any(o["status"] not in TERMINAL for o in mine if o["broker_id"]):
            time.sleep(0.1)
            self.poll()
        res = self._result(mine, side, lots, c, qty, limit)
        with self.lock:
            for o in mine:                              # anything still working is announced when it fills
                o["acked"] = True
        return res

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
            if o.get("acked") and self.notify and new in ("COMPLETE", "REJECTED"):
                word = ("BOUGHT" if o["side"] == "B" else "SOLD") if new == "COMPLETE" else "REJECTED"
                lots = o["qty"] // max(1, o["lot"])
                self.notify({"ok": new == "COMPLETE",
                             "msg": f"{word} {lots} lot{'s' if lots != 1 else ''} {o['symbol']}"
                                    + (f" @ {o['avg']:,.2f}" if new == "COMPLETE" else f": {o['reason']}")})
            audit.write("fill" if new == "COMPLETE" else "order-status",
                        what=f"Kotak {o['broker_id']} {o['side']} {o['symbol']} qty {o['qty']} -> {new}",
                        filled=o["filled"], avg=o["avg"], reason=o["reason"] if new != "COMPLETE" else "")
        return changed

    def _poll_loop(self):
        while True:
            try:
                with self.lock:
                    working = any(o["status"] not in TERMINAL for o in self.orders)
                burst = time.time() < self._burst_until
                self._poll_kick.wait(BURST_POLL_S if burst else FAST_POLL_S if working else SLOW_POLL_S)
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
        audit.write("modify", what=f"Kotak {o['broker_id']} {o['symbol']} price -> {price:g}", result=err or "sent")
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
        audit.write("cancel", what=f"Kotak {o['broker_id']} {o['symbol']}", result=err or "sent")
        if err:
            return {"ok": False, "msg": f"Kotak refused the cancel: {err}"}
        return {"ok": True, "msg": f"cancel sent for #{oid} (Kotak {o['broker_id']})"}

    def cancel_all(self) -> int:
        with self.lock:
            ids = [o["id"] for o in self.orders if o["status"] not in TERMINAL and o.get("broker_id")]
        return sum(1 for i in ids if self.cancel(i)["ok"])

    # ── exits: always against what KOTAK holds ──────────────────────────────
    def _exit_price_fallback(self, token, side):
        """No live quote on this contract yet: price off Kotak's REST LTP, 2 % through it."""
        c = self.u.by_token[token]
        r = self._call("quotes", instrument_tokens=[{"instrument_token": str(token), "exchange_segment": SEG}],
                       quote_type="ltp")
        try:
            ltp = float((r[0] if isinstance(r, list) else r["message"][0])["ltp"])
        except Exception:
            return None
        return max(_round_tick(ltp * (1.02 if side == "B" else 0.98), c.tick, side == "B"), c.tick) if ltp else None

    def _exit_leg(self, token, prod, net, fraction, source):
        c = self.u.by_token[token]
        lots = int(abs(net) // c.lot * fraction + 1e-9) or 1
        qty = min(lots * c.lot, abs(net))
        side = "S" if net > 0 else "B"
        r = self.place(token, side, qty, "MKT", source=source, product=prod)
        if not r["ok"] and "no price" in r.get("msg", ""):
            lim = self._exit_price_fallback(token, side)
            if lim:
                r = self.place(token, side, qty, "LMT", lim, source=source, product=prod)
        return r

    def _legs(self, tokens=None):
        with self.lock:
            return [(t, prod, n) for t, p in self.pos.items() if tokens is None or t in tokens
                    for prod, n in (p.get("by_prod") or {}).items() if n]

    def _send_parallel(self, legs, fraction, source, timeout=12.0):
        """Closing shorts (buys) first, then closing longs (sells); each group sent at the same time."""
        out = []
        for group in ([l for l in legs if l[2] < 0], [l for l in legs if l[2] > 0]):
            if not group:
                continue
            futs = [self._pool.submit(self._exit_leg, t, prod, n, fraction, source) for t, prod, n in group]
            done, pending = fwait(futs, timeout=timeout)
            out += [f.result() if not f.exception() else {"ok": False, "msg": f"{f.exception()}"} for f in done]
            out += [{"ok": False, "msg": "no reply from Kotak yet (still trying)"} for _ in pending]
        return out

    def exit_position(self, token, fraction: float = 1.0, source="exit") -> dict:
        try:
            self.sync_positions(raise_errors=True)          # act on what Kotak holds NOW
        except Exception as e:
            return {"ok": False, "msg": f"not sent - cannot read Kotak positions: {e}"}
        legs = self._legs({token})
        if not legs:
            return {"ok": False, "msg": "Kotak shows no open position in this contract"}
        res = self._send_parallel(legs, fraction, source)
        bad = [r["msg"] for r in res if not r["ok"]]
        if len(res) == 1:
            return res[0]
        return {"ok": not bad, "state": "filled" if all(r.get("state") == "filled" for r in res) else "waiting",
                "msg": " · ".join(r["msg"] for r in res)}

    def _cancel_all_broker(self, name: str | None = None) -> int:
        """Cancel EVERY working MCX order on the account (terminal's and manual) - or only those of one
        instrument (`name`, matched through the contract list so GOLD never catches GOLDM) - all at once."""
        try:
            rows = self._rows()
        except Exception as e:
            log.warning("exit all: cannot read Kotak orders to cancel: %s", e)
            return 0
        ids = [str(_dig(r, *_OID_KEYS)) for r in rows
               if "mcx" in str(r.get("exSeg", "mcx")).lower() and _dig(r, *_OID_KEYS)
               and not any(w in str(r.get("ordSt", "")).lower() for w in BROKER_DONE)
               and (name is None or getattr(self.by_symbol.get(str(r.get("trdSym", ""))), "name", None) == name)]
        if not ids:
            return 0
        futs = [self._pool.submit(self._call, "cancel_order", order_id=i) for i in ids]
        fwait(futs, timeout=8)
        audit.write("exit-all", what=f"cancel sent for {len(ids)} working MCX order(s)", ids=ids)
        self._poll_kick.set()
        return len(ids)

    def exit_all(self, fraction: float = 1.0) -> dict:
        """FLATTEN THE BROKER: every open MCX position on Kotak P, verified against Kotak."""
        return self._flatten(fraction, None)

    def exit_instrument(self, name: str, fraction: float = 1.0) -> dict:
        """The same, for one instrument only (Exit 25/50/75/100 % row and its timers)."""
        return self._flatten(fraction, name)

    def _flatten(self, fraction: float, name: str | None) -> dict:
        what = name or "ALL"
        src = "exit all" if name is None else f"exit {name}"
        t0 = time.time()
        try:
            self.sync_positions(raise_errors=True)
        except Exception as e:
            audit.write("exit-all", what=f"EXIT {what} STOPPED: cannot read Kotak positions", detail=str(e)[:200])
            return {"ok": False, "msg": f"EXIT {what} NOT SENT - cannot read Kotak positions ({e}). Square off in Kotak."}
        toks = None if name is None else {t for t, c in self.u.by_token.items() if c.name == name}
        n_cancel = self._cancel_all_broker(name)
        legs = self._legs(toks)
        audit.write("exit-all", what=f"EXIT {what} {int(fraction * 100)}%: {len(legs)} Kotak position(s)",
                    legs=[f"{self.u.by_token[t].symbol} {p} {n}" for t, p, n in legs])
        if not legs:
            return {"ok": True, "state": "filled", "msg": f"Kotak shows no open {'MCX' if name is None else name} position" +
                    (f" · {n_cancel} working order(s) cancelled" if n_cancel else "")}
        res = self._send_parallel(legs, fraction, src)
        bad = [r["msg"] for r in res if not r["ok"]]
        # verify: re-read Kotak until flat; push any exit still working after EXIT_PUSH_S through the book
        pushed, left = set(), legs
        t_sent = time.time()                            # the check window starts once everything is sent
        first = True
        while first or time.time() - t_sent < EXIT_PUSH_S + EXIT_VERIFY_S:
            first = False
            time.sleep(0.5)
            self.poll()
            if time.time() - t_sent >= EXIT_PUSH_S:
                with self.lock:
                    slow = [o for o in self.orders if o["status"] == "OPEN" and o.get("source") == src
                            and o.get("broker_id") and o["id"] not in pushed]
                for o in slow:
                    lim = self._aggressive(o["token"], o["side"]) or self._exit_price_fallback(o["token"], o["side"])
                    pushed.add(o["id"])
                    if lim:
                        self._pool.submit(self._modify, o, lim)
            try:
                self.sync_positions(raise_errors=True)
            except Exception:
                continue
            left = self._legs(toks)
            if fraction < 1.0 or not left:
                break
        secs = time.time() - t0
        if fraction >= 1.0 and not left:
            msg = (f"FLAT - Kotak confirms 0 open {'MCX' if name is None else name} positions "
                   f"({len(legs)} closed in {secs:.1f}s)")
            audit.write("exit-all", what=msg)
            return {"ok": True, "state": "filled", "msg": msg + (f" · {n_cancel} order(s) cancelled" if n_cancel else "")}
        if fraction < 1.0:
            msg = f"exit {int(fraction * 100)}% {'' if name is None else name + ' '}sent for {len(legs)} position(s)"
            audit.write("exit-all", what=msg, problems=bad)
            return {"ok": not bad, "state": "filled" if not bad else "waiting",
                    "msg": msg + (f" · PROBLEM: {'; '.join(bad)}" if bad else "")}
        names = ", ".join(f"{self.u.by_token[t].symbol} {n}" for t, _, n in left)
        msg = f"NOT FLAT after {secs:.0f}s - still open at Kotak: {names}" + (f" · {'; '.join(bad)}" if bad else "")
        audit.write("exit-all", what=msg)
        return {"ok": False, "state": "waiting", "msg": msg}

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
