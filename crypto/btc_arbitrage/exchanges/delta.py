"""
exchanges/delta.py — Delta Exchange India, BTCUSD perpetual, public data only.
=============================================================================

Verified live 2026-10-03:

  REST  GET https://api.india.delta.exchange/v2/products/BTCUSD
        contract_type "perpetual_futures", contract_value "0.001" (BTC),
        quoting/settling asset USD, taker_commission_rate "0.0005"
  WS    wss://socket.india.delta.exchange
        subscribe {"type":"subscribe","payload":{"channels":[
                    {"name":"l1_orderbook","symbols":["BTCUSD"]},
                    {"name":"funding_rate","symbols":["BTCUSD"]}]}}

  l1_orderbook message (sizes are CONTRACTS, timestamps MICROseconds):
    {"type":"l1_orderbook","symbol":"BTCUSD","best_bid":"84012.5","bid_qty":"2621",
     "best_ask":"84013.0","ask_qty":"3562","timestamp":1790968808420232,
     "last_updated_at":1790968808376292,"last_sequence_no":10072595,"product_id":27}
    -> pushed ~2-4 times a second

  l2_updates (order book: snapshot, then incremental updates) — see core/orderbook.py

  funding_rate message (rates in PERCENT per funding interval):
    {"type":"funding_rate","symbol":"BTCUSD","funding_rate":0.0039,
     "funding_interval":28800,"next_funding_realization":1790985600000000, ...}

`{"type":"enable_heartbeat"}` asks Delta for a heartbeat every 30 s, so a quiet
book still proves the socket is alive.
"""

import json

import requests

from .base import run_socket
from core.orderbook import DeltaBook

NAME = "delta"


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_l1(msg: dict, symbol: str, contract_value_btc: float):
    """-> (bid, bid_qty_btc, ask, ask_qty_btc, exchange_ts_ms) or None."""
    if msg.get("type") != "l1_orderbook" or msg.get("symbol") != symbol:
        return None
    bid, ask = _f(msg.get("best_bid")), _f(msg.get("best_ask"))
    bq, aq = _f(msg.get("bid_qty")), _f(msg.get("ask_qty"))
    ts = _f(msg.get("last_updated_at")) or _f(msg.get("timestamp"))
    return (bid, (bq or 0) * contract_value_btc, ask, (aq or 0) * contract_value_btc,
            ts / 1000 if ts else None)


def parse_funding(msg: dict, symbol: str):
    """-> (rate_fraction, interval_hours, next_ms) or None."""
    if msg.get("type") != "funding_rate" or msg.get("symbol") != symbol:
        return None
    r = _f(msg.get("funding_rate"))
    iv = _f(msg.get("funding_interval"))
    nxt = _f(msg.get("next_funding_realization"))
    return (r / 100 if r is not None else None, iv / 3600 if iv else None,
            nxt / 1000 if nxt else None)


def verify_contract(cfg, log) -> dict:
    """Refuse to run against anything but the configured BTC perpetual.
    Returns the product so the caller can compare the live taker fee."""
    url = f"{cfg.DELTA_REST}/v2/products/{cfg.DELTA_SYMBOL}"
    r = requests.get(url, headers={"User-Agent": cfg.USER_AGENT}, timeout=20)
    r.raise_for_status()
    p = r.json().get("result") or {}
    want = cfg.DELTA_CONTRACT
    problems = []
    if p.get("contract_type") != want["contract_type"]:
        problems.append(f"contract_type {p.get('contract_type')} != {want['contract_type']}")
    if (p.get("underlying_asset") or {}).get("symbol") != want["underlying"]:
        problems.append(f"underlying {(p.get('underlying_asset') or {}).get('symbol')}")
    if _f(p.get("contract_value")) != want["contract_value_btc"]:
        problems.append(f"contract_value {p.get('contract_value')} != {want['contract_value_btc']}")
    if (p.get("quoting_asset") or {}).get("symbol") != want["quote"]:
        problems.append(f"quote {(p.get('quoting_asset') or {}).get('symbol')} != {want['quote']}")
    if p.get("state") not in (None, "live"):
        problems.append(f"state {p.get('state')}")
    if problems:
        raise RuntimeError(f"Delta {cfg.DELTA_SYMBOL} is not the expected contract: " + "; ".join(problems))
    live_fee = _f(p.get("taker_commission_rate"))
    if live_fee is not None and abs(live_fee - cfg.DELTA_TAKER_FEE) > 1e-9:
        log.warning(f"delta: live taker fee {live_fee} differs from config DELTA_TAKER_FEE "
                    f"{cfg.DELTA_TAKER_FEE} — config is used; update it if the exchange changed")
    log.info(f"delta: verified {cfg.DELTA_SYMBOL} perpetual, contract_value "
             f"{p.get('contract_value')} BTC, quote USD, live taker fee {live_fee}")
    return p


async def run(cfg, md, log, stop):
    sym, cv = cfg.DELTA_SYMBOL, cfg.DELTA_CONTRACT["contract_value_btc"]
    book = DeltaBook(cv)

    async def on_open(ws):
        book.reset()
        await ws.send(json.dumps({"type": "subscribe", "payload": {"channels": [
            {"name": "l1_orderbook", "symbols": [sym]},
            {"name": "l2_updates", "symbols": [sym]},
            {"name": "funding_rate", "symbols": [sym]}]}}))
        await ws.send(json.dumps({"type": "enable_heartbeat"}))

    def on_message(msg):
        q = parse_l1(msg, sym, cv)
        if q:
            md.put(NAME, *q)
            return
        if msg.get("type") == "l2_updates" and msg.get("symbol") == sym:
            if book.apply(msg):             # may raise ResyncRequired -> socket rebuilt
                md.put_depth(NAME, book.top_bids, book.top_asks, book.ts_ms)
            return
        fr = parse_funding(msg, sym)
        if fr:
            f = md.delta
            f.funding_rate, f.funding_interval_h, f.next_funding_ms = fr
            return
        if msg.get("type") == "error" or msg.get("error"):
            md.delta.last_error = json.dumps(msg)[:300]
            log.warning(f"delta: server error {json.dumps(msg)[:300]}")

    await run_socket(NAME, cfg.DELTA_WS, md, log, on_open, on_message,
                     cfg.STALE_SOCKET_S, stop, on_close=book.reset)
