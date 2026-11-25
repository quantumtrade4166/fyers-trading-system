"""
cepe_imbalance/live.py — REAL orders on Kotak D for the "{NAME}_LIVE" books (NIFTY_LIVE, SENSEX_LIVE).
======================================================================================================

⚠️ LIVE MONEY. The strategy is the unchanged NORMAL rules (strategy.CePeImbalance). The only change is
the book: instead of paper fills at the live bid/ask, KotakLiveBook.order() sends a real marketable LIMIT
order to Kotak D (= Kotak P, env keys *_D, the same Kotak login this engine already uses for its feed)
and books ONLY what Kotak reports as filled, at Kotak's own average price.

OWN BOOK. Other strategies (Nifty ST Directional, the NSE desk) trade NIFTY options on the same Kotak D
account, so the book is ONLY its own fills, matched by Kotak order NUMBER (Kotak's order book does not
return tags — 13-Nov-2026 lesson). Kotak's positions are read every few seconds as a safety net:
  * a buy-back is never bigger than what Kotak holds short in that contract (read after our last fill)
  * Kotak holding LESS than the book = fault -> the engine flattens what is there and stops the book

ORDERS. Marketable LIMIT (Kotak takes no MARKET order on options through the API):
  attempt 1  priced off our own live tick: SELL bid - cushion / BUY ask + cushion (cushion = max(2 ticks, 1%));
             the exchange fills at the resting price, the cushion only covers a quote that moves
  attempt 2+ cancel the unfilled rest, re-price from Kotak's own 5-level depth (through the size, inside
             the exchange price band); SELL 3 attempts, BUY 6 (a buy-back must get done)
  an order whose state Kotak cannot confirm is UNSETTLED: no new order is sent until it is settled
  (an unseen fill must never be doubled)

SAFETY.
  * new risk (a SELL that adds to a short) only while the book is ARMED (data/live/control.json, owner only);
    buy-backs of open shorts are always allowed; this book never goes long
  * any order that cannot be completed sets `fault`: the engine buys everything back and stops the book
  * an exit that cannot complete stays `pending_exit` and is retried every few seconds until flat
  * max loss = the strategy's MTM stop (mtm_stop, default Rs 5,000 per index); 15:15 square-off; a live
    book holding positions at square-off time is squared off even when it was stopped by hand
  * restart: the book reloads its own file (positions + every order), unsettled orders are re-checked by
    order number; open positions left from an earlier day are carried in and flagged
"""
import datetime as dt
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance.book import IST, PaperBook, Position, _retry, atomic_write, today

log = logging.getLogger("cepe.live")

PRODUCT = "NRML"
FREEZE = {"NIFTY": 1800, "SENSEX": 1000}      # max units per order (exchange freeze quantity)
CUSHION_PCT = 0.01                            # attempt 1: 1% through our own touch
CUSHION_TICKS = 2
QUOTE_FRESH_S = 3.0                           # our tick must be this fresh to price attempt 1
WAIT_FIRST_S = 2.0                            # a marketable limit on a NIFTY / SENSEX option fills in ms
WAIT_RETRY_S = 3.0
CONFIRM_S = 5.0                               # after a cancel: time to learn the order's final state
SETTLE_EVERY_S = 2.0                          # unsettled orders: re-read Kotak this often (live worker, 23-Nov fix)
POLL_S = 0.15                                 # order status poll while waiting
ATTEMPTS = {"SELL": 3, "BUY": 6}
ACCOUNT_POLL_S = 3.0                          # Kotak positions + limits
POS_STALE_S = 20.0
POS_AFTER_FILL_S = 2.5                        # Kotak's positions lag our fill: trust them only after this
EXIT_RETRY_S = 3.0
NO_REPLY_GIVE_UP_S = 60.0                     # a sent order with no reply and not in Kotak's book -> never placed
LOST_WAIT_S = 10.0                            # an order with no clear reply: keep looking for it in Kotak's book

_OID_KEYS = ("nOrdNo", "orderId", "order_id", "OrderNumber", "orderNumber", "ordNo")
_MARGIN_WORDS = ("margin", "insufficient", "shortfall", "rms reject", "exposure", "limit exceeded", "pwr reject")


# ── small helpers (Kotak reply parsing, same rules as mcx_terminal/live.py) ──
def txt(x) -> str:
    """str() that never raises (the Kotak SDK's ApiException.__str__ returns None)."""
    if isinstance(x, BaseException):
        parts = []
        for a in ("status", "reason", "body", "message"):
            try:
                v = getattr(x, a, None)
            except Exception:
                v = None
            if v not in (None, "", 0):
                parts.append(v if isinstance(v, str) else repr(v))
        try:
            parts += [a if isinstance(a, str) else repr(a) for a in x.args if a not in (None, "")]
        except Exception:
            pass
        return f"{type(x).__name__}: " + " | ".join(dict.fromkeys(parts)) if parts else type(x).__name__
    try:
        s = str(x)
        if isinstance(s, str):
            return s
    except Exception:
        pass
    try:
        return repr(x)
    except Exception:
        return type(x).__name__


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
    if not isinstance(resp, dict):
        return f"unexpected reply: {txt(resp)[:160]}"
    for k in ("Error", "error", "Error Message"):
        if resp.get(k):
            return txt(resp[k])[:200]
    if extract_oid(resp):
        return None
    d = resp.get("data") if isinstance(resp.get("data"), dict) else resp
    stat = str(d.get("stat", "")).strip().lower()
    if d.get("errMsg") or (stat and stat not in ("ok", "success")):
        return txt(d.get("errMsg") or d.get("stat") or f"stCode={d.get('stCode')}")[:200]
    return None


_SESSION_RE = re.compile(r"2fa|session|unauthori[sz]|invalid token|invalid jwt|token expired|jwt expired|\b401\b")
RELOGIN_COOLDOWN_S = 60.0


def session_error(r) -> bool:
    """Did Kotak say our session is gone? Only an ERROR reply counts. 16-Nov bug: the old check searched the
    whole reply text, so a normal positions/order reply holding "401" inside a number (order no.
    2611160000-2401-0, a price, a token) forced a fresh login - 260 logins in one day, each one dropping
    the price feed."""
    if isinstance(r, list):
        return False
    if isinstance(r, dict):
        if isinstance(r.get("data"), list) or extract_oid(r):
            return False
        err = resp_error(r)
        code = str(r.get("stCode") or r.get("code") or "")
        if not err and code != "401":
            return False
        text = f"{err or ''} {code}"
    else:
        text = txt(r)
    return bool(_SESSION_RE.search(text.lower()))


_UNCLEAR = ("timeout", "time out", "timed out", "time-out", "read timed", "connection aborted", "connection reset",
            "connection error", "remote end closed", "remotedisconnected", "max retries", "bad gateway",
            "gateway time", "service unavailable", "temporarily unavailable", "no response", " 502", " 503", " 504")


def is_timeout(text) -> bool:
    """An order reply that does NOT say whether Kotak took the order. 25-Nov-2026: Kotak answered two BUYs with
    "time out" (with a space - the old check only knew "timeout" / "timed out"), the book took that as a refusal
    and retried, and Kotak had filled all three -> a 3-lot LONG the book never knew about. Such a reply is now
    UNKNOWN: the order is looked for in Kotak's order book, never re-sent blind."""
    t = " " + str(text).lower()
    return any(w in t for w in _UNCLEAR)


def is_margin(text) -> bool:
    t = str(text or "").lower()
    return any(w in t for w in _MARGIN_WORDS)


def round_tick(px: float, tick: float) -> float:
    tick = tick or 0.05
    return round(round(float(px) / tick) * tick, 2)


def status_word(st) -> str:
    """Kotak ordSt -> COMPLETE / REJECTED / CANCELLED / OPEN ('incomplete' is NOT complete)."""
    s = str(st or "").strip().lower()
    if ("complete" in s and "incomplete" not in s) or s in ("traded", "filled"):
        return "COMPLETE"
    if "reject" in s:
        return "REJECTED"
    if "cancel" in s and "pending" not in s:
        return "CANCELLED"
    return "OPEN" if s else ""


TERMINAL = ("COMPLETE", "REJECTED", "CANCELLED")


# ── arm switch (owner only) ──────────────────────────────────────────────────
class Control:
    """data/live/control.json = {book: {"armed": bool, "updated": "...", "by": "..."}}."""

    def __init__(self, path=None):
        self.path = path or (C.DATA / "live" / "control.json")
        self.lock = threading.Lock()

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except ValueError:
            log.exception("live control unreadable -> everything DISARMED")
            return {}

    def armed(self, name) -> bool:
        return bool((self.read().get(name) or {}).get("armed"))

    def set(self, name, armed: bool, by="screen") -> dict:
        with self.lock:
            d = self.read()
            d[name] = {"armed": bool(armed), "updated": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), "by": by}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self.path, json.dumps(d, indent=1))
            log.warning("LIVE %s %s (%s)", name, "ARMED" if armed else "disarmed", by)
            return d[name]


# ── Kotak D account: client, positions (safety net) and margin ──────────────
class Account:
    def __init__(self, sessions=None, client=None, start=True):
        """sessions: mcx_terminal.kotak_session.SessionHolder (Kotak P/D). client: test hook (a fake)."""
        self.sessions = sessions
        self._client_override = client
        self.lock = threading.Lock()
        self.pos: dict = {}                 # "seg|token" -> net units (Kotak's whole account)
        self.pos_at = 0.0
        self.pos_msg = "not read yet"
        self.limits: dict = {}              # {"used", "net", "at"}
        self.limits_msg = "not read yet"
        self._kick = threading.Event()
        self._last_relogin = 0.0
        if start:
            threading.Thread(target=self._loop, daemon=True, name="CepeLiveAccount").start()

    def client(self):
        return self._client_override or self.sessions.get().client

    def call(self, fn_name, **kw):
        """One Kotak REST call; a session-looking failure logs in again once and retries."""
        try:
            r = getattr(self.client(), fn_name)(**kw)
        except Exception as e:
            r = {"Error": txt(e)}
        if self._client_override is None and session_error(r):
            if time.time() - self._last_relogin < RELOGIN_COOLDOWN_S:
                return r                                  # never a login storm: one per minute at most
            self._last_relogin = time.time()
            log.warning("Kotak %s says the session expired (%s) - logging in again", fn_name, txt(r)[:160])
            try:
                self.sessions.get(force=True)
                r = getattr(self.client(), fn_name)(**kw)
            except Exception as e:
                r = {"Error": txt(e)}
        return r

    # positions
    def read_positions(self) -> bool:
        t0 = time.time()                                      # the read reflects fills only from before t0
        r = self.call("positions")
        rows = r if isinstance(r, list) else (r.get("data") if isinstance(r, dict) else None)
        if not isinstance(rows, list):
            msg = txt((r or {}).get("errMsg") or (r or {}).get("Error") or r) if isinstance(r, dict) else txt(r)
            if "no data" in msg.lower():
                rows = []                                     # Kotak's way of saying: no positions today
            else:
                with self.lock:
                    self.pos_msg = f"cannot read Kotak positions: {msg[:160]}"
                return False
        new = {}
        for x in rows:
            n = lambda k: _f(x.get(k)) or 0.0
            net = n("cfBuyQty") + n("flBuyQty") - n("cfSellQty") - n("flSellQty")
            try:
                key = f"{str(x.get('exSeg', '')).lower()}|{int(x.get('tok'))}"
            except (TypeError, ValueError):
                continue
            new[key] = new.get(key, 0) + int(round(net))
        with self.lock:
            self.pos, self.pos_at, self.pos_msg = new, t0, ""
        return True

    def short_held(self, key, after_ts=0.0):
        """Units Kotak holds SHORT in `key`, or None when the read is stale / older than `after_ts`."""
        with self.lock:
            if not self.pos_at or time.time() - self.pos_at > POS_STALE_S or self.pos_at <= after_ts:
                return None
            n = self.pos.get(key, 0)
        return -n if n < 0 else 0

    # margin
    def read_limits(self) -> bool:
        r = self.call("limits")
        if not isinstance(r, dict) or resp_error(r) and not _f(r.get("Net")):
            with self.lock:
                self.limits_msg = f"cannot read Kotak limits: {txt(r)[:160]}"
            return False
        with self.lock:
            self.limits = {"used": _f(r.get("MarginUsed")) or 0.0, "net": _f(r.get("Net")) or 0.0,
                           "collateral": _f(r.get("RmsCollateral")) or 0.0, "at": time.time()}
            self.limits_msg = ""
        return True

    def margin_used(self):
        with self.lock:
            return self.limits.get("used") if self.limits.get("at") else None

    def _loop(self):
        while True:
            try:
                self._kick.wait(ACCOUNT_POLL_S)
                self._kick.clear()
                self.read_positions()
                self.read_limits()
            except Exception:
                log.exception("live account poll")
                time.sleep(2)

    def kick(self):
        self._kick.set()

    def view(self) -> dict:
        with self.lock:
            lim = dict(self.limits)
            return {"positions_ok": bool(self.pos_at) and time.time() - self.pos_at <= POS_STALE_S and not self.pos_msg,
                    "positions_age": round(time.time() - self.pos_at, 1) if self.pos_at else None,
                    "positions_msg": self.pos_msg,
                    "margin_used": lim.get("used"), "net": lim.get("net"), "collateral": lim.get("collateral"),
                    "limits_age": round(time.time() - lim["at"], 1) if lim.get("at") else None,
                    "limits_msg": self.limits_msg}


class LiveCtx:
    """What the engine needs to build live books: one Kotak D account + the arm switch."""

    def __init__(self, account, control=None, sleep=time.sleep):
        self.account = account
        self.control = control or Control()
        self.sleep = sleep


# ── the live book ────────────────────────────────────────────────────────────
class KotakLiveBook(PaperBook):
    live = True

    def __init__(self, name, market, ctx: LiveCtx, date=None, persist=True):
        self.ctx = ctx
        self.account = ctx.account
        self.orders: list = []              # every order sent today (on disk BEFORE it goes to Kotak)
        self.unsettled: dict = {}           # order id / tag -> order whose final state Kotak hasn't confirmed
        self.fault = None                   # reason the book must be flattened + stopped (engine acts on it)
        self.pending_exit = None            # {"reason", "since"}: exits still owed to Kotak
        self.last_exit_try = 0.0
        self.last_fill_ts: dict = {}        # key -> time of our last fill (positions lag behind it)
        self.margin_before = None           # account MarginUsed just before this book's entry
        self.carried: list = []             # positions carried in from an earlier day (shown as an alarm)
        self._seq = 0
        self._olock = threading.RLock()     # one order sequence at a time
        self._slock = threading.Lock()      # _settle() runs once at a time (exit legs run in parallel)
        self._last_settle = 0.0             # recheck_unsettled(): last background re-read
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="CepeLiveExit")
        super().__init__(name, market, date, persist)

    # ── files ───────────────────────────────────────────────────────────────
    def _live_path(self):
        return C.DATA / "books" / self.date / f"{self.name}_live.json"

    def _load(self):
        super()._load()
        p = self._live_path()
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                self.orders = d.get("orders") or []
                self.unsettled = d.get("unsettled") or {}
                self.fault = d.get("fault")
                self.pending_exit = d.get("pending_exit")
                self.margin_before = d.get("margin_before")
                self.carried = d.get("carried") or []
                self.last_fill_ts = d.get("last_fill_ts") or {}
            except ValueError:
                log.exception("%s live file unreadable", self.name)
                self.fault = "live order file unreadable after restart - check Kotak by hand"
        if not (C.DATA / "books" / self.date / f"{self.name}.json").exists():
            self._carry_in()

    def _carry_in(self):
        """A new day with no file yet: open shorts left on an EARLIER day (an exit that never completed)
        come over so they are still managed / squared off. Flagged on the screen."""
        root = C.DATA / "books"
        if not root.exists():
            return
        prev = sorted(d.name for d in root.iterdir() if d.is_dir() and d.name < self.date
                      and (d / f"{self.name}.json").exists())
        if not prev:
            return
        try:
            doc = json.loads((root / prev[-1] / f"{self.name}.json").read_text(encoding="utf-8"))
        except ValueError:
            return
        for k, v in (doc.get("positions") or {}).items():
            if not v.get("lots"):
                continue
            p = Position(**{**v, "realized": 0.0})
            self.positions[k] = p
            self.carried.append(f"{p.symbol} {p.lots:g} lots from {prev[-1]}")
        if self.carried:
            self.fault = "positions carried in from " + prev[-1] + ": " + "; ".join(self.carried)
            log.error("%s %s", self.name, self.fault)
            self._save(None)

    def _save(self, trade):
        super()._save(trade)
        if not self.persist:
            return
        atomic_write(self._live_path(), json.dumps(
            {"name": self.name, "date": self.date, "orders": self.orders[-3000:], "unsettled": self.unsettled,
             "fault": self.fault, "pending_exit": self.pending_exit, "margin_before": self.margin_before,
             "carried": self.carried, "last_fill_ts": self.last_fill_ts}, default=str))

    # ── pricing ─────────────────────────────────────────────────────────────
    def fill_price(self, key, side):
        """Strategy pre-check (execute): a live two-sided quote must exist (same as paper)."""
        return super().fill_price(key, side)

    def _first_price(self, c, side):
        q = self.market.quote(c.key)
        if not q or not q.recv_ts or time.time() - q.recv_ts > QUOTE_FRESH_S:
            return None, "no fresh tick"
        ref = (q.bid if side == "SELL" else q.ask) or q.ltp
        if not ref or ref <= 0:
            return None, "no touch"
        cush = max(CUSHION_TICKS * (c.tick or 0.05), ref * CUSHION_PCT)
        px = ref - cush if side == "SELL" else ref + cush
        return max(round_tick(px, c.tick), c.tick or 0.05), f"tick {'bid' if side == 'SELL' else 'ask'} {ref:g}"

    def _depth_price(self, c, side, qty, attempt):
        """Through Kotak's own 5-level depth for `qty` + cushion, clamped inside the price band."""
        cushion = CUSHION_TICKS * attempt
        try:
            r = self.account.call("quotes", instrument_tokens=[{"instrument_token": str(c.token),
                                                                 "exchange_segment": c.seg}], quote_type="all")
            row = r[0] if isinstance(r, list) and r and isinstance(r[0], dict) else None
        except Exception:
            row = None
        q = self.market.quote(c.key)
        tick = c.tick or 0.05
        fallback = None
        if q and (q.bid or q.ask or q.ltp):
            ref = (q.bid if side == "SELL" else q.ask) or q.ltp
            fallback = ref * (1 - 0.03 * attempt) if side == "SELL" else ref * (1 + 0.03 * attempt)
        if not row:
            return (max(round_tick(fallback, tick), tick), "fallback (no depth)") if fallback else (None, "no price")
        d = row.get("depth") or {}
        levels = d.get("buy" if side == "SELL" else "sell") or []
        need, last = qty, None
        for lv in levels:
            px = _f(lv.get("price"))
            if not px:
                continue
            last = px
            need -= int(_f(lv.get("quantity")) or 0)
            if need <= 0:
                break
        if last is None:
            last = fallback
            if last is None:
                return None, "empty depth"
        px = last - cushion * tick if side == "SELL" else last + cushion * tick
        lo, hi = _f(row.get("low_price_range")), _f(row.get("high_price_range"))
        if side == "SELL" and lo:
            px = max(px, lo)
        if side == "BUY" and hi:
            px = min(px, hi)
        return max(round_tick(px, tick), tick), "depth"

    # ── Kotak order plumbing ────────────────────────────────────────────────
    def _tag(self) -> str:
        self._seq = (self._seq + 1) % 1000
        return f"{C.LIVE_TAG}{dt.datetime.now(IST):%H%M%S}{self._seq:03d}"

    def _rows(self):
        r = self.account.call("order_report")
        if isinstance(r, dict):
            d = r.get("data")
            if isinstance(d, list):
                return d
            if r.get("Error") or r.get("error"):
                raise RuntimeError(txt(r.get("Error") or r.get("error"))[:200])
            return []
        if isinstance(r, list):
            return r
        raise RuntimeError(f"unexpected order report: {txt(r)[:120]}")

    def _apply_row(self, o, row):
        o["broker_status"] = str(row.get("ordSt", ""))
        fld = int(_f(_dig(row, "fldQty", "filledQty")) or 0)
        avg = _f(row.get("avgPrc")) or 0.0
        if fld >= o["filled"]:
            o["filled"], o["avg"] = fld, avg or o.get("avg") or 0.0
        w = status_word(row.get("ordSt"))
        if fld >= o["qty"]:
            w = "COMPLETE"
        if w:
            o["status"] = w
        if w == "REJECTED":
            o["reason"] = f"Kotak rejected: {row.get('rejRsn') or row.get('ordSt')}"[:240]

    def _match_lost(self, o, rows):
        """A sent order whose reply never came: find it by exactly what we sent (Kotak returns no tags)."""
        side = "B" if o["side"] == "BUY" else "S"
        taken = {x.get("oid") for x in self.orders if x.get("oid")}
        for r in rows:
            oid = str(_dig(r, *_OID_KEYS) or "")
            if not oid or oid in taken:
                continue
            if (str(r.get("trdSym", "")) == o["symbol"] and str(r.get("trnsTp", "")).upper()[:1] == side
                    and int(_f(r.get("qty")) or 0) == o["qty"]
                    and abs((_f(r.get("prc")) or -1) - float(o["limit"])) < 0.005):
                return oid, r
        return None, None

    def _refresh(self, o) -> bool:
        """Read Kotak's order book once for order `o`. -> read OK?"""
        try:
            rows = self._rows()
        except Exception as e:
            o["read_error"] = txt(e)[:160]
            return False
        if not o.get("oid"):
            oid, row = self._match_lost(o, rows)
            if oid:
                o["oid"] = oid
                o["reason"] = "Kotak id found in its order book (reply was lost)"
                self._apply_row(o, row)
            elif time.time() - o["t0"] > NO_REPLY_GIVE_UP_S:
                o["status"], o["reason"] = "REJECTED", "no reply from Kotak and not in its order book"
            return True
        for r in rows:
            if str(_dig(r, *_OID_KEYS) or "") == o["oid"]:
                self._apply_row(o, r)
                return True
        return True

    def _wait(self, o, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._refresh(o)
            if o["status"] in TERMINAL:
                return
            self.ctx.sleep(POLL_S)

    def _send(self, c, side, qty, px, reason, attempt, src):
        o = {"t0": time.time(), "time": dt.datetime.now(IST).strftime("%H:%M:%S.%f")[:-3], "key": c.key,
             "symbol": c.symbol, "kind": c.kind, "strike": c.strike, "side": side, "qty": int(qty),
             "limit": px, "price_src": src, "attempt": attempt, "reason": reason, "tag": self._tag(),
             "oid": None, "status": "SENDING", "filled": 0, "avg": 0.0, "booked": 0}
        with self.lock:
            self.orders.append(o)
            self._save(None)                                  # on disk BEFORE it goes to Kotak
        t = time.time()
        r = self.account.call("place_order", exchange_segment=c.seg, product=PRODUCT, price=f"{px:.2f}",
                              order_type="L", quantity=str(int(qty)), validity="DAY", trading_symbol=c.symbol,
                              transaction_type="S" if side == "SELL" else "B", tag=o["tag"])
        o["ms_ack"] = round((time.time() - t) * 1000)
        err, oid = resp_error(r), extract_oid(r)
        if oid and not err:
            o.update(oid=oid, status="OPEN")
        elif not oid and (not err or is_timeout(err)):
            # 25-Nov fix: "time out" / no order id = UNKNOWN (Kotak may well have taken it) - stays SENDING and
            # is looked for in Kotak's order book; nothing is re-sent until it is found or proven absent
            o["reason"] = f"no clear reply from Kotak ({err or 'no order id'}, {o['ms_ack'] / 1000:.1f}s) - checking its order book"
        else:
            o.update(status="REJECTED", reason=f"Kotak refused: {err or 'no order id'}")
        log.warning("LIVE %s %s %s %d @ %s (try %d, %s) -> %s %s (%d ms)", self.name, side, c.symbol, qty, px,
                    attempt, src, oid, err or "", o["ms_ack"])
        with self.lock:
            self._save(None)
        return o

    def _cancel(self, o):
        if o.get("oid"):
            r = self.account.call("cancel_order", order_id=str(o["oid"]))
            o["cancel"] = resp_error(r) or "sent"

    # ── booking fills ───────────────────────────────────────────────────────
    def _book(self, c, side, units, px, reason, extra=None):
        """Our position changes by exactly what Kotak filled (units), at Kotak's price."""
        if units <= 0:
            return None
        lots = units / c.lot if c.lot else units
        lots = int(round(lots)) if abs(lots - round(lots)) < 1e-9 else round(lots, 6)
        with self.lock:
            p = self.positions.get(c.key)
            if p is None:
                p = self.positions[c.key] = Position(key=c.key, symbol=c.symbol, kind=c.kind, strike=c.strike,
                                                     expiry=c.expiry, unit_value=c.lot * c.mult)
            signed = lots if side == "BUY" else -lots
            before = p.lots
            if before == 0 or (before > 0) == (signed > 0):
                p.avg = (p.avg * abs(before) + px * lots) / (abs(before) + lots)
                p.lots = before + signed
            else:
                closing = min(abs(before), lots)
                sign = 1 if before > 0 else -1
                p.realized += (px - p.avg) * closing * sign * p.unit_value
                p.lots = before + signed
                if abs(p.lots) < 1e-9:
                    p.lots, p.avg = 0, 0.0
                elif (p.lots > 0) != (before > 0):
                    p.avg = px
            if isinstance(p.lots, float) and abs(p.lots - round(p.lots)) < 1e-9:
                p.lots = int(round(p.lots))
            q = self.market.quote(c.key)
            t = {"ok": True, "ts": time.time(), "time": dt.datetime.now(IST).strftime("%H:%M:%S.%f")[:-3],
                 "name": self.name, "key": c.key, "symbol": c.symbol, "kind": c.kind, "strike": c.strike,
                 "expiry": c.expiry, "side": side, "lots": lots, "units": units, "price": round(px, 4),
                 "ltp": q.ltp if q else None, "bid": q.bid if q else None, "ask": q.ask if q else None,
                 "pos_after": p.lots, "reason": reason, "live": True, **(extra or {})}
            self.trades.append(t)
            self.last_fill_ts[c.key] = time.time()
            self._save(t)
        self.account.kick()
        return t

    def _settle(self):
        """Orders whose state was unknown: read them again; book any fill not booked yet."""
        with self._slock:
            self._settle_locked()

    def recheck_unsettled(self) -> list:
        """23-Nov-2026 fix: an order Kotak could not confirm used to be re-read ONLY when the next order / exit /
        Start came. At the 09:15 open Kotak filled a SELL 25950 CE but its order book showed nothing for 7 s; the
        book halted thinking it held nothing, and the short sat at Kotak for 5 h 20 min until Stop was pressed.
        Now the live worker calls this every SETTLE_EVERY_S while anything is unsettled. -> trades booked now."""
        if not self.unsettled or time.time() - self._last_settle < SETTLE_EVERY_S:
            return []
        self._last_settle = time.time()
        n = len(self.trades)
        try:
            self._settle()
        except Exception as e:
            log.warning("%s re-check of unsettled orders failed: %s", self.name, txt(e)[:160])
            return []
        return self.trades[n:]

    def _settle_locked(self):
        for k, o in list(self.unsettled.items()):
            if o["status"] not in TERMINAL:
                self._refresh(o)
                if o["status"] not in TERMINAL and o.get("oid"):
                    self._cancel(o)
                    self._refresh(o)
            if o["status"] in TERMINAL:
                c = self.market.u.by_key.get(o["key"])
                extra = o["filled"] - o.get("booked", 0)
                if extra > 0 and c is not None:
                    self._book(c, o["side"], extra, o["avg"] or o["limit"], o["reason"] + " (settled late)",
                               {"order_ids": [o["oid"]], "settled": True})
                    o["booked"] = o["filled"]
                del self.unsettled[k]
                log.warning("%s settled order %s: %s filled %s", self.name, o.get("oid") or o["tag"], o["status"], o["filled"])
                with self.lock:
                    self._save(None)

    # ── one complete order: send, wait, re-price, until done or out of tries ──
    def _execute(self, c, side, units, reason):
        """-> (filled units, avg price, error or None, info). Books every fill as it is seen."""
        t_dec = time.time()
        mark_dec = self.market.mark(c.key)
        filled, value, err, oids, tries = 0, 0.0, None, [], []
        freeze = FREEZE.get(C.und_of(self.name), 10 ** 9)
        attempts = ATTEMPTS[side]
        for a in range(1, attempts + 1):
            rem = units - filled
            if rem <= 0:
                break
            chunk = min(rem, freeze - freeze % max(1, c.lot) or c.lot)
            px, src = self._first_price(c, side) if a == 1 else (None, "")
            if px is None:
                px, src = self._depth_price(c, side, chunk, a)
            if px is None:
                err = f"no price for {c.symbol} ({src})"
                self.ctx.sleep(0.3)
                continue
            o = self._send(c, side, chunk, px, reason, a, src)
            if o["status"] != "REJECTED":
                self._wait(o, WAIT_FIRST_S if a == 1 else WAIT_RETRY_S)
                if o["status"] not in TERMINAL:
                    self._cancel(o)
                    self._wait(o, CONFIRM_S)                 # a fill can land while cancelling
                if o["status"] not in TERMINAL and not o.get("oid"):
                    self._wait(o, LOST_WAIT_S)               # 25-Nov: no clear reply - keep looking in Kotak's book
                    if o.get("oid") and o["status"] not in TERMINAL:
                        self._cancel(o)
                        self._wait(o, CONFIRM_S)
            got = o["filled"] - o["booked"]
            if got > 0:
                t = self._book(c, side, got, o["avg"] or px, reason, {"order_ids": [o["oid"]]})
                o["booked"] = o["filled"]
                filled += got
                value += got * (o["avg"] or px)
                if t:
                    tries.append(t)
            oids.append(o.get("oid") or o["tag"])
            if o["status"] not in TERMINAL:
                self.unsettled[o.get("oid") or o["tag"]] = o
                err = f"UNSETTLED order {o.get('oid') or o['tag']} {c.symbol}: Kotak did not confirm cancel/fill"
                log.error("%s %s", self.name, err)
                break
            if o["status"] == "REJECTED":
                err = o.get("reason") or "rejected"
                if is_margin(err):
                    err = "MARGIN: " + err
                    break
                continue
            if o["filled"] < o["qty"]:
                err = f"{c.symbol} {side} {o['filled']}/{o['qty']} filled at {px:g} ({src}) - re-pricing"
        with self.lock:
            self._save(None)
        avg = value / filled if filled else None
        info = {"order_ids": oids, "attempts": len(oids), "mark_at_decision": mark_dec,
                "ms_total": round((time.time() - t_dec) * 1000)}
        if avg and mark_dec:
            info["slip"] = round((mark_dec - avg) if side == "SELL" else (avg - mark_dec), 2)   # + = worse than mark
        return filled, avg, (err if filled < units else None), info

    # ── the strategy's order entry point ───────────────────────────────────
    def order(self, contract, side: str, lots, reason: str = "", price=None) -> dict:
        side = str(side).upper()
        if side not in ("BUY", "SELL") or not lots or float(lots) <= 0:
            return {"ok": False, "error": f"bad order {side} {lots}"}
        with self._olock:
            return self._order(contract, side, float(lots), reason)

    def _order(self, c, side, lots, reason):
        with self.lock:
            p = self.positions.get(c.key)
            cur = p.lots if p else 0
        reduces = (cur < 0 and side == "BUY" and lots <= -cur + 1e-9) or (cur > 0 and side == "SELL" and lots <= cur + 1e-9)
        if not reduces:
            if side == "BUY":
                return {"ok": False, "error": f"refused: BUY {lots:g} {c.symbol} would open a LONG (book holds {cur:g})"}
            if self.fault:
                return {"ok": False, "error": f"refused: book halted - {self.fault}"}
            if not self.ctx.control.armed(self.name):
                return {"ok": False, "error": "refused: LIVE is not armed"}
        if self.unsettled:
            self._settle()
            if self.unsettled:
                return {"ok": False, "error": f"refused: {len(self.unsettled)} unsettled order(s) at Kotak - waiting for their final state"}
        units = int(round(lots * c.lot))
        if reduces:
            held = self.account.short_held(c.key, after_ts=self.last_fill_ts.get(c.key, 0) + POS_AFTER_FILL_S) \
                if side == "BUY" else None
            if held is not None and held < units:
                self.fault = (f"Kotak holds {held} short of {c.symbol}, this book {units} - closed outside the book?"
                              f" buying back only {held}")
                log.error("%s %s", self.name, self.fault)
                units = held
                if units <= 0:
                    with self.lock:                          # nothing to buy at Kotak: the book's lots are gone
                        self._book(c, side, int(round(lots * c.lot)), self.market.mark(c.key) or p.avg,
                                   reason + " (not at Kotak - written off the book)", {"not_at_broker": True})
                    return {"ok": False, "error": self.fault}
        elif side == "SELL" and not any(x.lots for x in self.positions.values()):
            if self.account.read_limits():                   # margin before this book's entry
                self.margin_before = self.account.margin_used()
        filled, avg, err, info = self._execute(c, side, units, reason)
        if err or filled < units:
            msg = f"{side} {c.symbol}: {filled}/{units} filled - {err or 'not filled'}"
            self.fault = self.fault or msg
            log.error("%s LIVE ORDER FAILED %s", self.name, msg)
            with self.lock:
                self._save(None)
            return {"ok": False, "error": msg, "filled_units": filled, "price": avg, **info}
        q = self.market.quote(c.key)
        return {"ok": True, "name": self.name, "key": c.key, "symbol": c.symbol, "kind": c.kind,
                "strike": c.strike, "expiry": c.expiry, "side": side, "lots": lots, "units": filled,
                "price": round(avg, 4), "ltp": q.ltp if q else None, "bid": q.bid if q else None,
                "ask": q.ask if q else None, "reason": reason, "live": True, **info}

    # ── exits: all legs at once, retried until Kotak is flat ────────────────
    def exit_all(self, reason: str = "exit all") -> list:
        with self._olock:
            self.last_exit_try = time.time()
            if self.unsettled:
                self._settle()
            with self.lock:
                opens = [p for p in self.positions.values() if p.lots]
            legs = []
            for p in sorted(opens, key=lambda p: p.lots):              # shorts first
                c = self.market.u.by_key.get(p.key)
                if c is None:
                    legs.append(None)
                    continue
                legs.append((c, "BUY" if p.lots < 0 else "SELL", abs(p.lots)))
            futs = [self._pool.submit(self._order, *leg, reason) if leg else None for leg in legs]
            out = []
            for f in futs:
                try:
                    out.append(f.result(timeout=90) if f else {"ok": False, "error": "unknown contract"})
                except Exception as e:
                    out.append({"ok": False, "error": txt(e)})
            with self.lock:
                still = [p.symbol for p in self.positions.values() if p.lots]
                if still:
                    self.pending_exit = self.pending_exit or {"reason": reason, "since": dt.datetime.now(IST).strftime("%H:%M:%S")}
                    self.pending_exit["left"] = still
                    log.error("%s EXIT INCOMPLETE (%s): still open %s - retrying", self.name, reason, still)
                else:
                    self.pending_exit = None
                self._save(None)
            return out

    def retry_exit(self):
        if self.pending_exit and time.time() - self.last_exit_try >= EXIT_RETRY_S:
            self.exit_all(self.pending_exit.get("reason", "exit retry") + " (retry)")

    def clear_fault(self):
        """Owner action (Start after a fault): only when the book is flat and nothing is unsettled."""
        with self.lock:
            if any(p.lots for p in self.positions.values()) or self.unsettled or self.pending_exit:
                return False
            self.fault = None
            self.carried = []
            self._save(None)
            return True

    # ── numbers for the screen ──────────────────────────────────────────────
    def charges(self) -> float:
        """Estimated statutory charges + brokerage for today's fills (NSE rates; SENSEX close enough)."""
        try:
            from backtesting.options_pivot_intraday.costs import statutory_costs
        except Exception:
            return 0.0
        by = {}
        for t in self.trades:
            v = by.setdefault(t["symbol"], {"S": [0, 0.0, 0], "B": [0, 0.0, 0]})
            k = "S" if t["side"] == "SELL" else "B"
            u = int(t.get("units") or 0)
            v[k][0] += u
            v[k][1] += u * float(t["price"])
            v[k][2] += 1
        est = 0.0
        for sym, v in by.items():
            q = max(v["S"][0], v["B"][0])
            if not q:
                continue
            sp = v["S"][1] / v["S"][0] if v["S"][0] else 0.0
            bp = v["B"][1] / v["B"][0] if v["B"][0] else 0.0
            try:
                cst = statutory_costs(sp, bp, q, self.date)
                est += cst["total"] + 20.0 * max(0, v["S"][2] + v["B"][2] - 2)   # extra orders pay brokerage too
            except Exception:
                pass
        return round(est, 2)

    def live_view(self) -> dict:
        acct = self.account.view()
        with self.lock:
            has = any(p.lots for p in self.positions.values())
        used = acct.get("margin_used")
        book_m = round(used - self.margin_before, 2) if (has and used is not None and self.margin_before is not None) else None
        recent = [{k: o.get(k) for k in ("time", "symbol", "side", "qty", "limit", "price_src", "attempt", "oid",
                                         "status", "filled", "avg", "ms_ack", "reason")} for o in self.orders[-60:]]
        lat = [o.get("ms_ack") for o in self.orders if o.get("ms_ack") is not None][-50:]
        return {"armed": self.ctx.control.armed(self.name), "fault": self.fault, "pending_exit": self.pending_exit,
                "unsettled": len(self.unsettled), "carried": self.carried, "orders": len(self.orders),
                "orders_recent": recent, "book_margin": book_m, "margin_before": self.margin_before,
                "charges_est": self.charges(), "ack_ms_median": sorted(lat)[len(lat) // 2] if lat else None,
                "account": acct}
