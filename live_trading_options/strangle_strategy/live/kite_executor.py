"""
live/kite_executor.py — Zerodha (Kite) order placement for the strangle.
========================================================================

The ONLY module that talks to the broker's order API. Reuses the existing Kite
auth (KITE_API_KEY in .env + deployment/zerodha_token.json, valid for today).

Symbol resolution is done by LOOKING UP the exact contract in Kite's instrument
dump — never by hand-building a tradingsymbol. A wrong symbol = a real order on the
wrong contract, so this is the correctness-critical piece and it must come from the
broker's own list.

SAFETY: place_market() places a REAL order. It is never called by resolution or
validation code; only the live executor (behind the enabled+mode=live gate) calls it.
"""

import os
import json
import datetime as dt
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]                 # G:\fyers_data_pipeline
_TOKEN_FILE = REPO / "deployment" / "zerodha_token.json"

# load deployment/.env when run standalone (the dashboard already loads it)
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / "deployment" / ".env")
except Exception:
    pass
_API_KEY = os.getenv("KITE_API_KEY", "")

# index -> (Kite options exchange, instrument `name`)
EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}
KITE_NAME = {"NIFTY": "NIFTY", "SENSEX": "SENSEX"}

BUY, SELL = "BUY", "SELL"


def access_token() -> str:
    """Today's Kite access token (raises if missing/stale — never trades on an old token)."""
    if not _TOKEN_FILE.exists():
        raise RuntimeError(f"no Kite token file at {_TOKEN_FILE}")
    d = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    if d.get("date") != dt.date.today().isoformat():
        raise RuntimeError(f"Kite token is for {d.get('date')}, not today — run zerodha_login")
    return d.get("access_token", "")


# Kite's client default read-timeout is 7s — too tight for order placement during
# open-auction / high-load minutes, where a POST can be slow but still SUCCEEDS. A too-
# short timeout abandons a request whose order may already be in the OMS. 15s is safe.
_HTTP_TIMEOUT = 15


def get_kite():
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=_API_KEY, timeout=_HTTP_TIMEOUT)
    k.set_access_token(access_token())
    return k


_instr_cache: dict[str, list] = {}


def _instruments(kite, exchange: str) -> list:
    """Kite's full contract list for an exchange (cached for the process — it's a
    big daily dump)."""
    if exchange not in _instr_cache:
        _instr_cache[exchange] = kite.instruments(exchange)
    return _instr_cache[exchange]


def resolve(kite, index: str, expiry, strike: int, opt_type: str) -> dict:
    """Exact Kite contract for (index, expiry, strike, CE/PE), from the instrument
    dump. Returns {tradingsymbol, exchange, instrument_token, lot_size}. Raises if
    no exact match (better to fail loudly than trade a guessed symbol)."""
    exch, name = EXCHANGE[index], KITE_NAME[index]
    exp = str(expiry)
    for r in _instruments(kite, exch):
        if (r.get("name") == name and r.get("instrument_type") == opt_type
                and int(r.get("strike", 0)) == int(strike)
                and str(r.get("expiry")) == exp):
            return {"tradingsymbol": r["tradingsymbol"], "exchange": exch,
                    "instrument_token": r["instrument_token"], "lot_size": r["lot_size"]}
    raise RuntimeError(f"no Kite contract: {index} {exp} {strike} {opt_type}")


def place_market(kite, tradingsymbol: str, exchange: str, side: str, qty: int,
                 product: str = "MIS", tag: str = "vwstrangle") -> str:
    """Place a MARKET order and return its order_id. ⚠️ REAL ORDER.
    NOTE: Zerodha REJECTS market orders for options (NFO/BFO) — use place_limit for
    the strangle legs. Kept only for non-option instruments."""
    return kite.place_order(
        variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol,
        transaction_type=side, quantity=qty, product=product,
        order_type=kite.ORDER_TYPE_MARKET, tag=tag)


def _round_tick(price: float, tick: float = 0.05) -> float:
    """Options trade on a 0.05 tick — Kite rejects off-tick limit prices."""
    return round(round(price / tick) * tick, 2)


# ── broker error classification ───────────────────────────────────────────
# A rejected order must never escape as a bare exception. On 2026-08-25 a
# replacement leg was refused for a Rs662 margin shortfall on a Rs35.6L
# requirement; the exception unwound out of the strategy, so nothing reached the
# snapshot the dashboard renders, and the position sat SINGLE-LEGGED and
# unattended until the user happened to look at the broker terminal.

MARGIN_MARKERS = ("insufficient funds", "margin required", "margin available",
                  "insufficient margin", "margin shortfall", "available margin")


def is_margin_error(exc: BaseException | str) -> bool:
    """True when a broker error is a margin/funds shortfall.

    Kite reports these as GeneralException or InputException with the detail only
    in the message, so the text is all there is to go on."""
    msg = str(exc or "").lower()
    return any(m in msg for m in MARGIN_MARKERS)


# ── order-book depth ──────────────────────────────────────────────────────
# Pricing a marketable limit off a MULTIPLE of the last mark is a guess: too tight
# and it will not fill, too wide and it can sweep a thin book and fill somewhere
# terrible. The book itself is the honest answer — it says what is actually on
# offer and in what size. One REST quote is cheap, and these paths are rare.

def quote_book(kite, exchange: str, tradingsymbol: str) -> dict | None:
    """Top-of-book + 5-level depth for one contract, or None if it cannot be read.

    Returns {bid, ask, ltp, buy:[levels], sell:[levels]} where each level is
    {price, quantity}. Never raises — the caller must be able to fall back."""
    key = f"{exchange}:{tradingsymbol}"
    try:
        row = (kite.quote([key]) or {}).get(key) or {}
    except Exception:
        return None
    d = row.get("depth") or {}
    buy = [{"price": l.get("price"), "quantity": l.get("quantity", 0)}
           for l in (d.get("buy") or []) if l.get("price")]
    sell = [{"price": l.get("price"), "quantity": l.get("quantity", 0)}
            for l in (d.get("sell") or []) if l.get("price")]
    if not buy and not sell and not row.get("last_price"):
        return None
    return {"bid": buy[0]["price"] if buy else None,
            "ask": sell[0]["price"] if sell else None,
            "ltp": row.get("last_price"), "buy": buy, "sell": sell}


def sweep_price(levels: list, qty: int, side: str, cushion_ticks: int = 2,
                tick: float = 0.05) -> float | None:
    """The price that would actually clear `qty` against `levels`, plus a cushion.

    Walks the real book instead of guessing. For a BUY pass the SELL levels (you
    lift offers); for a SELL pass the BUY levels. If `qty` is larger than the five
    visible levels, the deepest visible price is used — still anchored to something
    real, and the cushion carries the rest.

    Returns None when the book is empty, so the caller falls back to its own
    estimate rather than sending a price built from nothing."""
    if not levels:
        return None
    need, last = qty, None
    for lvl in levels:
        px = lvl.get("price")
        if not px:
            continue
        last = px
        need -= (lvl.get("quantity") or 0)
        if need <= 0:
            break
    if last is None:
        return None
    cushion = max(0, cushion_ticks) * tick
    return _round_tick(last + cushion if side == BUY else last - cushion)


def marketable_price(kite, exchange: str, tradingsymbol: str, side: str, qty: int,
                     fallback: float = None, cushion_ticks: int = 2) -> float | None:
    """Depth-derived marketable limit for `qty`, falling back to `fallback`.

    This is the price to send when you need the order to FILL: it clears the
    visible size and adds a few ticks, rather than multiplying a stale mark."""
    book = quote_book(kite, exchange, tradingsymbol)
    if book:
        levels = book["sell"] if side == BUY else book["buy"]
        px = sweep_price(levels, qty, side, cushion_ticks=cushion_ticks)
        if px and px > 0:
            return px
    return fallback


def place_limit(kite, tradingsymbol: str, exchange: str, side: str, qty: int,
                price: float, product: str = "MIS", tag: str = "vwstrangle") -> str:
    """Place a LIMIT order (the ONLY order type Zerodha allows for options). ⚠️ REAL
    ORDER. Pass a MARKETABLE price (through the touch) so it fills at the best bid/ask
    like a market order — `price` is only the worst-case cap, not the fill price."""
    return kite.place_order(
        variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol,
        transaction_type=side, quantity=qty, product=product,
        order_type=kite.ORDER_TYPE_LIMIT, price=_round_tick(price), tag=tag)


def _find_recent_order(kite, tradingsymbol: str, side: str, qty: int, since,
                       tag: str = "vwstrangle", wait: float = 3.0,
                       order_type: str = None) -> str | None:
    """Poll the order book briefly for an order we may have placed but whose HTTP
    response we LOST (timeout/network error). Matched by tag+symbol+side+qty placed
    at/after `since`. This is what makes a placement timeout safe — before we ever
    retry, we confirm whether the first request actually landed in the OMS.

    `order_type` narrows the match (e.g. "SL"), which matters once a strategy has
    BOTH a resting stop and a plain exit on the same leg: without it a lost stop
    placement could adopt the exit's order id."""
    import time as _t
    deadline = _t.monotonic() + wait
    while True:
        try:
            orders = kite.orders() or []
        except Exception:
            orders = []
        for o in orders:
            if (o.get("tag") == tag and o.get("tradingsymbol") == tradingsymbol
                    and o.get("transaction_type") == side
                    and int(o.get("quantity") or 0) == int(qty)
                    and (order_type is None or o.get("order_type") == order_type)
                    and o.get("status") not in ("REJECTED", "CANCELLED")):
                ots = o.get("order_timestamp")
                if since is not None and hasattr(ots, "year") and ots < since:
                    continue
                return o.get("order_id")
        if _t.monotonic() >= deadline:
            return None
        _t.sleep(0.7)


def place_limit_verified(kite, tradingsymbol: str, exchange: str, side: str, qty: int,
                         price: float, product: str = "MIS", tag: str = "vwstrangle",
                         retries: int = 2) -> str:
    """place_limit that survives a lost/timed-out HTTP response WITHOUT ever double-
    placing a real order. On a network error the outcome is UNKNOWN — the order may
    already be in the OMS — so we look it up (tag+symbol+side+qty, just now) before
    deciding: if found we adopt that order_id; only if nothing landed do we retry.
    Raises only if placement genuinely fails every attempt with nothing in the book."""
    import datetime as _dt
    import time as _t
    last_err = None
    for attempt in range(retries + 1):
        since = _dt.datetime.now() - _dt.timedelta(seconds=6)
        try:
            return place_limit(kite, tradingsymbol, exchange, side, qty, price, product, tag)
        except Exception as e:
            last_err = e
            # did the (timed-out) request actually place the order? poll the book.
            found = _find_recent_order(kite, tradingsymbol, side, qty, since, tag)
            if found:
                return found                       # it DID land — adopt it, never re-place
            if attempt < retries:
                _t.sleep(1.0)                      # nothing landed → safe to retry
    raise last_err


# ── resting stop-loss orders (used by the delta-neutral strangle) ─────────────
# A short option leg's stop is a BUY order that triggers ABOVE the entry premium.
# Zerodha rejects SL-M on options exactly as it rejects MARKET, so the stop must be
# SL: a trigger price plus a limit priced THROUGH the trigger so it actually fills
# when it fires. The limit is a worst-case cap, never the expected fill.
# Gap between the trigger and the limit, in POINTS: trigger 40 -> limit 42. The
# gap is what makes the stop actually fill — a limit sitting exactly at the trigger
# is jumped by any move that gaps through it, and the "stop" then protects nothing.
# Too wide and the fill is needlessly bad; 2 points suits Nifty-sized premiums.
# Overridable per strategy via the `buffer` argument.
_SL_LIMIT_BUFFER = 2.0


def sl_limit_price(trigger: float, side: str = BUY, buffer: float = None) -> float:
    """Limit price to pair with an SL trigger, rounded to the 0.05 option tick.
    A BUY stop needs limit ABOVE the trigger (Zerodha requires limit >= trigger);
    a SELL stop needs it below."""
    b = _SL_LIMIT_BUFFER if buffer is None else abs(float(buffer))
    return _round_tick(trigger + b) if side == BUY else _round_tick(max(0.05, trigger - b))


def place_sl(kite, tradingsymbol: str, exchange: str, side: str, qty: int,
             trigger: float, product: str = "MIS", tag: str = "dnstrangle",
             buffer: float = None) -> str:
    """Place a resting SL (stop-loss) order and return its order_id. ⚠️ REAL ORDER.
    It sits at the exchange until the trigger is hit, so it protects the position
    even if this engine, the dashboard, or the whole VPS dies."""
    return kite.place_order(
        variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol,
        transaction_type=side, quantity=qty, product=product,
        order_type=kite.ORDER_TYPE_SL, trigger_price=_round_tick(trigger),
        price=sl_limit_price(trigger, side, buffer), tag=tag)


def place_sl_verified(kite, tradingsymbol: str, exchange: str, side: str, qty: int,
                      trigger: float, product: str = "MIS", tag: str = "dnstrangle",
                      retries: int = 2, buffer: float = None) -> str:
    """place_sl that survives a lost/timed-out HTTP response without ever placing
    two stops on the same leg (which would buy back double on a trigger). Same
    look-before-retry contract as place_limit_verified, narrowed to SL orders."""
    import datetime as _dt
    import time as _t
    last_err = None
    for attempt in range(retries + 1):
        since = _dt.datetime.now() - _dt.timedelta(seconds=6)
        try:
            return place_sl(kite, tradingsymbol, exchange, side, qty, trigger,
                            product, tag, buffer)
        except Exception as e:
            last_err = e
            found = _find_recent_order(kite, tradingsymbol, side, qty, since, tag,
                                       order_type="SL")
            if found:
                return found                       # it DID land — adopt it, never re-place
            if attempt < retries:
                _t.sleep(1.0)
    raise last_err


def modify_sl(kite, order_id: str, trigger: float, buffer: float = None):
    """Move an existing resting SL to a new trigger (and re-derive its limit).

    MODIFY, never cancel-then-replace: a cancel leaves the leg unprotected for the
    round trip in between, and if the replacement is rejected the position is naked
    with nobody watching. Modifying keeps a stop in the book the whole time."""
    return kite.modify_order(
        variety=kite.VARIETY_REGULAR, order_id=order_id,
        trigger_price=_round_tick(trigger),
        price=sl_limit_price(trigger, BUY, buffer))


def is_resting(kite, order_id: str) -> bool:
    """True when the order is live at the exchange and still waiting (a stop that
    is actually protecting the leg). This is the check that must pass before a
    short position is treated as open."""
    try:
        hist = kite.order_history(order_id) or []
    except Exception:
        return False
    st = (hist[-1] if hist else {}).get("status")
    return st in ("TRIGGER PENDING", "OPEN", "OPEN PENDING", "VALIDATION PENDING")


def order_status(kite, order_id: str) -> dict:
    """Latest status + fill for an order_id: {status, filled_qty, avg_price, fill_time}.
    fill_time = the broker's exchange timestamp (HH:MM:SS) of the fill — used to show
    the EXACT per-leg fill time + any delay between the two legs. This is how the ledger
    reconciles against the broker — by order id, not the netted position.
    A transient query timeout returns status=None (still-pending) so the caller's poll
    loop simply tries again instead of crashing the whole entry."""
    try:
        hist = kite.order_history(order_id) or []
    except Exception:
        return {"status": None, "filled_qty": 0, "avg_price": None, "fill_time": None}
    last = hist[-1] if hist else {}
    ts = last.get("exchange_timestamp") or last.get("order_timestamp")
    try:
        ft = ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else (str(ts)[-8:] if ts else None)
    except Exception:
        ft = str(ts) if ts else None
    return {"status": last.get("status"), "filled_qty": last.get("filled_quantity", 0),
            "avg_price": last.get("average_price"), "fill_time": ft}


def cancel(kite, order_id: str):
    return kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)


def contract_for(kite, exchange: str, tradingsymbol: str) -> dict | None:
    """Reverse lookup: broker symbol -> {strike, opt_type, expiry, lot_size}.
    Needed when rebuilding state from the order book after a restart, where all we
    have is the tradingsymbol we traded. Reads the same cached instrument dump."""
    for r in _instruments(kite, exchange):
        if r.get("tradingsymbol") == tradingsymbol:
            return {"strike": int(r.get("strike", 0)),
                    "opt_type": r.get("instrument_type"),
                    "expiry": r.get("expiry"), "lot_size": r.get("lot_size")}
    return None


RESTING = ("TRIGGER PENDING", "OPEN", "OPEN PENDING", "VALIDATION PENDING")


def orders_by_id(kite, tag: str = "dnstrangle") -> dict:
    """{order_id: {...}} for every order THIS strategy placed today, in ONE call.

    One `kite.orders()` beats polling `order_history` per leg: it costs a single
    request no matter how many legs are live, which matters when this runs every
    couple of seconds all day. Used to answer both "did a stop fire?" and "is that
    stop still actually resting at the exchange?" from the same snapshot."""
    out = {}
    for o in (kite.orders() or []):
        if o.get("tag") != tag:
            continue
        ts = o.get("exchange_timestamp") or o.get("order_timestamp")
        out[o.get("order_id")] = {
            "status": o.get("status"), "order_type": o.get("order_type"),
            "tradingsymbol": o.get("tradingsymbol"), "exchange": o.get("exchange"),
            "side": o.get("transaction_type"),
            "qty": int(o.get("quantity") or 0),
            "filled_qty": int(o.get("filled_quantity") or 0),
            "avg_price": o.get("average_price"),
            "trigger_price": o.get("trigger_price"),
            "resting": o.get("status") in RESTING,
            "fill_time": ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else (str(ts)[-8:] if ts else None),
        }
    return out


def resting_stops(kite, tag: str = "dnstrangle") -> list:
    """This strategy's SL orders that are still waiting at the exchange.
    After a restart these are the proof that a recovered position is protected —
    without them the position would look naked and get covered unnecessarily."""
    out = []
    for o in (kite.orders() or []):
        if (o.get("tag") == tag and o.get("order_type") == "SL"
                and o.get("status") in ("TRIGGER PENDING", "OPEN", "OPEN PENDING")):
            out.append({"order_id": o.get("order_id"),
                        "tradingsymbol": o.get("tradingsymbol"),
                        "exchange": o.get("exchange"),
                        "trigger_price": o.get("trigger_price"),
                        "quantity": int(o.get("quantity") or 0)})
    return out


def strategy_fills(kite, tag: str = "vwstrangle") -> list:
    """Today's COMPLETE orders placed by THIS strategy, identified ONLY by our `tag`.
    The own-book source of truth for reconciliation — NEVER kite.positions() (which is
    netted and mixes the user's MANUAL trades on the same strike). Returns a list of
    {tradingsymbol, exchange, side, qty, avg_price, order_id, fill_time}."""
    out = []
    for o in (kite.orders() or []):
        if o.get("tag") != tag or o.get("status") != "COMPLETE":
            continue
        ts = o.get("exchange_timestamp") or o.get("order_timestamp")
        out.append({
            "tradingsymbol": o.get("tradingsymbol"),
            "exchange": o.get("exchange"),
            "side": o.get("transaction_type"),
            "qty": int(o.get("filled_quantity") or o.get("quantity") or 0),
            "avg_price": o.get("average_price"),
            "order_id": o.get("order_id"),
            "fill_time": ts.strftime("%H:%M:%S") if hasattr(ts, "strftime") else str(ts),
        })
    return out
