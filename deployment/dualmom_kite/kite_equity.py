"""
Kite (Zerodha) cash-equity adapter for DualMom. The only module that talks to Kite.

SESSION: reads today's access token from deployment/zerodha_token.json. It NEVER
logs in - the VPS generates the token each morning and the Vwap Strangle / DN
engine use the same one. A login here would burn a TOTP attempt.

OWN BOOK: every read that feeds DualMom is filtered to orders whose tag starts
with config.ORDER_TAG. The account is shared; nothing else is ours.
"""

import json
import math
import os
import random
import threading
import time
from datetime import date, datetime
from pathlib import Path

from deployment.dualmom_kite import config as C

TOKEN_FILE = Path(__file__).resolve().parents[1] / "zerodha_token.json"

_client_lock = threading.Lock()
_client = {"token": None, "kite": None}


# ── session (read-only) ──────────────────────────────────────────────────────

def read_token() -> str:
    """Today's token, or '' if the morning login has not written it yet."""
    try:
        d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if d.get("date") != date.today().isoformat():
        return ""
    return d.get("access_token", "") or ""


def client():
    """A KiteConnect bound to TODAY's token. Rebuilt when the token file changes."""
    tok = read_token()
    if not tok:
        raise RuntimeError("no Kite access token for today yet (the VPS morning login "
                           "writes deployment/zerodha_token.json) - DualMom never logs "
                           "in itself")
    with _client_lock:
        if _client["token"] != tok:
            from kiteconnect import KiteConnect
            k = KiteConnect(api_key=os.getenv("KITE_API_KEY", ""))
            k.set_access_token(tok)
            _client.update(token=tok, kite=k)
        return _client["kite"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x, default=0):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return default


def round_tick(px: float, tick: float) -> float:
    t = float(tick) if tick else 0.05
    return round(round(px / t) * t, 2)


def marketable_limit(mark: float, side: str, buf: float, tick: float) -> float:
    """Price THROUGH the touch; a cap, not the price paid."""
    px = mark * (1 + buf) if side == "BUY" else mark * (1 - buf)
    t = float(tick) if tick else 0.05
    return max(round_tick(px, t), t)


_tag_lock = threading.Lock()
_tag_seq = [0]


def unique_tag(prefix: str = None) -> str:
    """prefix + ddHHMMSS + NNN, e.g. dmk21093015007. Never repeats in a process."""
    prefix = prefix or C.ORDER_TAG
    with _tag_lock:
        _tag_seq[0] = (_tag_seq[0] + 1) % 1000
        n = _tag_seq[0]
    return f"{prefix}{datetime.now():%d%H%M%S}{n:03d}"


def is_ours(tag) -> bool:
    t = str(tag or "")
    return t.startswith(C.ORDER_TAG) and t[len(C.ORDER_TAG):].isdigit()


def order_tag(o: dict) -> str:
    t = o.get("tag")
    if not t and o.get("tags"):
        t = (o.get("tags") or [None])[0]
    return t or ""


# ── instruments ──────────────────────────────────────────────────────────────

_instr = {"day": None, "by_sym": {}}


def _load_instruments(kite):
    if _instr["day"] == date.today() and _instr["by_sym"]:
        return _instr["by_sym"]
    rows = kite.instruments(C.EXCHANGE)
    by = {}
    for r in rows:
        if str(r.get("segment", "")).upper() != "NSE" or str(r.get("instrument_type", "")).upper() != "EQ":
            continue
        by[str(r["tradingsymbol"]).upper()] = r
    _instr.update(day=date.today(), by_sym=by)
    return by


def resolve(kite, symbol: str) -> dict:
    """Exact match only: SYMBOL (EQ series) or SYMBOL-BE (trade-for-trade).

    Same rule as Kotak: never a substring match, never another series."""
    by = _load_instruments(kite)
    s = str(symbol).upper()
    for ts, series in ((s, "EQ"), (f"{s}-BE", "BE")):
        r = by.get(ts)
        if r:
            return {"symbol": s, "trading_symbol": ts, "series": series,
                    "tick_size": _f(r.get("tick_size"), 0.05) or 0.05,
                    "token": r.get("instrument_token"),
                    "is_trade_for_trade": series == "BE"}
    raise LookupError(f"{s}: no NSE EQ/BE instrument in the Kite master")


def base_symbol(trading_symbol: str) -> str:
    s = str(trading_symbol or "").upper()
    for suf in ("-BE", "-EQ"):
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


# ── market data ──────────────────────────────────────────────────────────────

def quotes(kite, trading_symbols) -> dict:
    """{trading_symbol: {ltp, prev_close, change, change_pct}} in batches of 200."""
    out, syms = {}, list(dict.fromkeys(trading_symbols))
    for i in range(0, len(syms), 200):
        keys = [f"{C.EXCHANGE}:{s}" for s in syms[i:i + 200]]
        try:
            data = kite.quote(keys) or {}
        except Exception:
            continue
        for k, v in data.items():
            ts = k.split(":", 1)[1]
            ltp = _f(v.get("last_price"))
            pc = _f((v.get("ohlc") or {}).get("close"))
            chg = ltp - pc if ltp and pc else _f(v.get("net_change"))
            out[ts] = {"ltp": ltp, "prev_close": pc, "change": round(chg, 4),
                       "change_pct": round(chg / pc * 100, 4) if pc else 0.0,
                       "upper_circuit": _f(v.get("upper_circuit_limit")) or None,
                       "lower_circuit": _f(v.get("lower_circuit_limit")) or None}
    return out


def clamp_to_circuit(px: float, side: str, tick: float, q: dict) -> float:
    """Keep a marketable limit inside the exchange price band. 22-Sep: CPPLUS and
    CEMPRO were REJECTED because mark +0.5% sat above the upper circuit."""
    uc, lc = (q or {}).get("upper_circuit"), (q or {}).get("lower_circuit")
    t = float(tick) if tick else 0.05
    if side == "BUY" and uc and px > uc:
        px = math.floor(uc / t + 1e-9) * t
    if side == "SELL" and lc and px < lc:
        px = math.ceil(lc / t - 1e-9) * t
    return round(px, 2)


def last_prices(kite, symbols) -> dict:
    """{symbol: ltp} for plain symbols, resolving each to its trading symbol."""
    ts_of = {}
    for s in symbols:
        try:
            ts_of[resolve(kite, s)["trading_symbol"]] = s
        except Exception:
            continue
    q = quotes(kite, list(ts_of))
    return {ts_of[ts]: v["ltp"] for ts, v in q.items() if v["ltp"] > 0 and ts in ts_of}


def free_margin(kite) -> dict:
    """Account-level equity margin (whole account - shown, never DualMom's cash)."""
    m = (kite.margins("equity") or {})
    av = m.get("available") or {}
    return {"net": _f(m.get("net")), "cash": _f(av.get("cash")),
            "live_balance": _f(av.get("live_balance")),
            "collateral": _f(av.get("collateral"))}


# ── broker positions (whole account; filtered by the caller) ─────────────────

def broker_equity_qty(kite) -> dict:
    """{symbol: {qty, settled, t1, today_buy, today_sell, trading_symbol}} for
    NSE CNC equity in the whole account. holdings() carries settled + T1 shares;
    today's buys/sells exist only in positions()['day']."""
    out = {}
    for h in kite.holdings() or []:
        if str(h.get("exchange", "")).upper() not in ("NSE", "BSE"):
            continue
        s = base_symbol(h.get("tradingsymbol"))
        p = out.setdefault(s, {"settled": 0, "t1": 0, "today_buy": 0, "today_sell": 0,
                               "trading_symbol": h.get("tradingsymbol")})
        p["settled"] += _i(h.get("quantity"))
        p["t1"] += _i(h.get("t1_quantity"))
    pos = (kite.positions() or {}).get("day") or []
    for r in pos:
        if str(r.get("product", "")).upper() != "CNC" or str(r.get("exchange", "")).upper() != "NSE":
            continue
        s = base_symbol(r.get("tradingsymbol"))
        p = out.setdefault(s, {"settled": 0, "t1": 0, "today_buy": 0, "today_sell": 0,
                               "trading_symbol": r.get("tradingsymbol")})
        p["today_buy"] += _i(r.get("buy_quantity"))
        p["today_sell"] += _i(r.get("sell_quantity"))
    for p in out.values():
        p["qty"] = p["settled"] + p["t1"] + p["today_buy"] - p["today_sell"]
    return out


# ── orders ───────────────────────────────────────────────────────────────────

def our_orders(kite) -> list:
    return [o for o in (kite.orders() or []) if is_ours(order_tag(o))]


def _find_by_tag(kite, tag: str):
    for o in kite.orders() or []:
        if order_tag(o) == tag:
            return o
    return None


def place_limit(kite, trading_symbol: str, side: str, qty: int, price: float,
                tick: float) -> dict:
    """One CNC LIMIT order with a unique tag. A lost HTTP response is resolved by
    LOOKING UP the tag, never by sending again."""
    px = round_tick(price, tick)
    for attempt in range(RATE_RETRIES + 1):
        res = _place_once(kite, trading_symbol, side, qty, px)
        # 22-Sep: IDEA and AEGISLOG REJECTED "Maximum allowed order requests per
        # second exceeded". The tag lookup has already proven the order does not
        # exist, so sending again after a pause cannot duplicate it.
        if res["status"] == "REJECTED" and _is_rate_limit(res.get("reason")) and attempt < RATE_RETRIES:
            time.sleep(1.0 + attempt)
            continue
        return res
    return res


RATE_RETRIES = 3
MIN_ORDER_GAP = 0.35            # s between orders; Kite allows 10/s, we stay far below
_last_send = [0.0]
_send_lock = threading.Lock()


def _is_rate_limit(msg) -> bool:
    m = str(msg or "").lower()
    return "requests per second" in m or "too many requests" in m or "rate limit" in m


def _place_once(kite, trading_symbol, side, qty, px) -> dict:
    tag = unique_tag()
    with _send_lock:
        wait = MIN_ORDER_GAP - (time.time() - _last_send[0])
        if wait > 0:
            time.sleep(wait)
        _last_send[0] = time.time()
    try:
        oid = kite.place_order(variety="regular", exchange=C.EXCHANGE,
                               tradingsymbol=trading_symbol, transaction_type=side,
                               quantity=int(qty), product=C.PRODUCT, order_type="LIMIT",
                               price=px, validity="DAY", tag=tag)
        return {"status": "PLACED", "order_id": str(oid), "tag": tag, "price": px}
    except Exception as e:
        msg = str(e) if str(e) else type(e).__name__
        # did it land anyway?
        for _ in range(3):
            time.sleep(1.0)
            try:
                o = _find_by_tag(kite, tag)
            except Exception:
                o = None
            if o:
                return {"status": "PLACED", "order_id": str(o["order_id"]), "tag": tag,
                        "price": px, "note": f"response lost ({msg}); found by tag"}
        margin = any(w in msg.lower() for w in ("margin", "insufficient", "funds"))
        return {"status": "REJECTED", "order_id": None, "tag": tag, "price": px,
                "reason": msg[:300], "margin": margin}


TERMINAL = {"COMPLETE", "REJECTED", "CANCELLED"}


def await_all(kite, order_ids, timeout: float = None, poll: float = None) -> dict:
    """Poll orders() (one call per round, not one per order) until every id is
    terminal. {order_id: {status, filled_qty, avg_price, reason}}."""
    timeout = C.ORDER_POLL_TIMEOUT if timeout is None else timeout
    poll = C.ORDER_POLL_SECONDS if poll is None else poll
    want = {str(x) for x in order_ids if x}
    out = {}
    deadline = time.time() + timeout
    while want and time.time() < deadline:
        try:
            book = {str(o["order_id"]): o for o in kite.orders() or []}
        except Exception:
            book = {}
        for oid in list(want):
            o = book.get(oid)
            if not o:
                continue
            st = str(o.get("status", "")).upper()
            out[oid] = {"status": st, "filled_qty": _i(o.get("filled_quantity")),
                        "avg_price": _f(o.get("average_price")),
                        "reason": o.get("status_message")}
            if st in TERMINAL:
                want.discard(oid)
        if want:
            time.sleep(poll + random.random() * 0.2)
    for oid in want:
        out.setdefault(oid, {"status": "UNKNOWN", "filled_qty": 0, "avg_price": 0.0,
                             "reason": "not terminal within timeout"})
    return out
