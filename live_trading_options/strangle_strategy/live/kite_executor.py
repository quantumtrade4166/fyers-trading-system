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


def get_kite():
    from kiteconnect import KiteConnect
    k = KiteConnect(api_key=_API_KEY)
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


def place_limit(kite, tradingsymbol: str, exchange: str, side: str, qty: int,
                price: float, product: str = "MIS", tag: str = "vwstrangle") -> str:
    """Place a LIMIT order (the ONLY order type Zerodha allows for options). ⚠️ REAL
    ORDER. Pass a MARKETABLE price (through the touch) so it fills at the best bid/ask
    like a market order — `price` is only the worst-case cap, not the fill price."""
    return kite.place_order(
        variety=kite.VARIETY_REGULAR, exchange=exchange, tradingsymbol=tradingsymbol,
        transaction_type=side, quantity=qty, product=product,
        order_type=kite.ORDER_TYPE_LIMIT, price=_round_tick(price), tag=tag)


def order_status(kite, order_id: str) -> dict:
    """Latest status + fill for an order_id: {status, filled_qty, avg_price, fill_time}.
    fill_time = the broker's exchange timestamp (HH:MM:SS) of the fill — used to show
    the EXACT per-leg fill time + any delay between the two legs. This is how the ledger
    reconciles against the broker — by order id, not the netted position."""
    hist = kite.order_history(order_id) or []
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
