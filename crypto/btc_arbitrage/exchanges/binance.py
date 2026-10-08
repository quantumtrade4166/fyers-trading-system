"""
exchanges/binance.py — Binance futures perpetual, public data only.
==================================================================

Verified live 2026-10-03:

  USD-M   REST https://fapi.binance.com/fapi/v1   WS wss://fstream.binance.com/ws
  COIN-M  REST https://dapi.binance.com/dapi/v1   WS wss://dstream.binance.com/ws

  exchangeInfo: BTCUSDT / BTCUSDC  contractType "PERPETUAL", baseAsset BTC
                BTCUSD_PERP (COIN-M) contractType "PERPETUAL", contractSize 100 (USD)

  <symbol>@bookTicker (real-time, every top-of-book change — tens a second):
    {"e":"bookTicker","u":11721678133241,"s":"BTCUSDT","b":"84012.90","B":"5.343",
     "a":"84013.00","A":"7.188","T":1790968808182,"E":1790968808182}
    USD-M: B/A are BTC.  COIN-M: B/A are CONTRACTS of $100.

  <symbol>@depth20@100ms (top 20 levels, complete, every 100 ms):
    {"e":"depthUpdate","s":"BTCUSDT","T":...,"b":[["84797.40","17.157"],...],"a":[...]}
  Both streams share one socket: /stream?streams=a/b wraps each as {"stream","data"}.

  Funding is polled over REST (premiumIndex + fundingInfo) once a minute —
  it changes every few hours, a socket for it is not worth the moving part.

Binance closes every socket after 24 h and sends pings every few minutes; the
websockets library answers the pings, and base.run_socket reconnects after the
24 h close like after any other drop.
"""

import json

import requests

from .base import run_socket

NAME = "binance"


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def to_btc(qty, price, qty_unit: str):
    if qty is None:
        return 0.0
    if qty_unit == "btc":
        return qty
    if qty_unit == "usd_100":                   # COIN-M: contracts of $100
        return qty * 100 / price if price else 0.0
    raise ValueError(f"unknown qty_unit {qty_unit!r}")


def parse_book_ticker(msg: dict, symbol: str, qty_unit: str):
    """-> (bid, bid_qty_btc, ask, ask_qty_btc, exchange_ts_ms) or None."""
    if msg.get("e") != "bookTicker" or msg.get("s") != symbol:
        return None
    bid, ask = _f(msg.get("b")), _f(msg.get("a"))
    return (bid, to_btc(_f(msg.get("B")), bid, qty_unit),
            ask, to_btc(_f(msg.get("A")), ask, qty_unit),
            _f(msg.get("T")) or _f(msg.get("E")))


def parse_depth(msg: dict, symbol: str, qty_unit: str):
    """<symbol>@depth20@100ms -> (bids, asks, exchange_ts_ms), sizes in BTC, best first.
    {"e":"depthUpdate","s":"BTCUSDT","T":...,"b":[["84797.40","17.157"],...],"a":[...]}"""
    if msg.get("e") != "depthUpdate" or msg.get("s") != symbol:
        return None
    bids = [(float(p), to_btc(float(q), float(p), qty_unit)) for p, q in msg.get("b", []) if float(q) > 0]
    asks = [(float(p), to_btc(float(q), float(p), qty_unit)) for p, q in msg.get("a", []) if float(q) > 0]
    bids.sort(key=lambda x: -x[0]); asks.sort(key=lambda x: x[0])
    return bids, asks, _f(msg.get("T")) or _f(msg.get("E"))


def contract(cfg) -> dict:
    c = cfg.BINANCE_CONTRACTS.get(cfg.BINANCE_SYMBOL)
    if not c:
        raise RuntimeError(f"BINANCE_SYMBOL {cfg.BINANCE_SYMBOL!r} is not in BINANCE_CONTRACTS "
                           f"({', '.join(cfg.BINANCE_CONTRACTS)}) — add it to the mapping first")
    return {**c, **cfg.BINANCE_ENDPOINTS[c["market"]]}


def _get(url, params=None, ua="x"):
    r = requests.get(url, params=params, headers={"User-Agent": ua}, timeout=20)
    if r.status_code in (418, 429):
        raise RuntimeError(f"Binance rate limit {r.status_code} on {url}")
    r.raise_for_status()
    return r.json()


def verify_contract(cfg, log) -> dict:
    c = contract(cfg)
    info = _get(f"{c['rest']}/exchangeInfo", ua=cfg.USER_AGENT)
    s = next((x for x in info.get("symbols", []) if x.get("symbol") == cfg.BINANCE_SYMBOL), None)
    if not s:
        raise RuntimeError(f"Binance {c['market']} does not list {cfg.BINANCE_SYMBOL}")
    problems = []
    if s.get("contractType") != "PERPETUAL":
        problems.append(f"contractType {s.get('contractType')} (Delta India only has a perpetual)")
    if s.get("baseAsset") != "BTC":
        problems.append(f"baseAsset {s.get('baseAsset')}")
    status = s.get("status") or s.get("contractStatus")
    if status != "TRADING":
        problems.append(f"status {status}")
    if c["market"] == "usdm" and s.get("quoteAsset") != c["quote"]:
        problems.append(f"quoteAsset {s.get('quoteAsset')} != {c['quote']}")
    if c["market"] == "coinm" and _f(s.get("contractSize")) != 100:
        problems.append(f"contractSize {s.get('contractSize')} != 100")
    if problems:
        raise RuntimeError(f"Binance {cfg.BINANCE_SYMBOL} is not the expected contract: " + "; ".join(problems))
    log.info(f"binance: verified {cfg.BINANCE_SYMBOL} {c['market']} perpetual, quote {c['quote']}")
    return s


def funding(cfg) -> tuple:
    """-> (rate_fraction, interval_hours, next_ms). REST, called once a minute."""
    c = contract(cfg)
    d = _get(f"{c['rest']}/premiumIndex", {"symbol": cfg.BINANCE_SYMBOL}, cfg.USER_AGENT)
    if isinstance(d, list):                     # COIN-M returns a list
        d = next((x for x in d if x.get("symbol") == cfg.BINANCE_SYMBOL), {})
    interval = 8.0
    try:
        fi = _get(f"{c['rest']}/fundingInfo", ua=cfg.USER_AGENT)
        hit = next((x for x in fi if x.get("symbol") == cfg.BINANCE_SYMBOL), None)
        if hit and hit.get("fundingIntervalHours"):
            interval = float(hit["fundingIntervalHours"])
    except Exception:
        pass                                    # symbols absent from fundingInfo use 8 h
    return _f(d.get("lastFundingRate")), interval, _f(d.get("nextFundingTime"))


def server_time_ms(cfg) -> float:
    c = contract(cfg)
    return float(_get(f"{c['rest']}/time", ua=cfg.USER_AGENT)["serverTime"])


def usdc_usdt(cfg):
    """Mid of USDC/USDT on Binance spot: how many USDT one USDC (~1 USD) buys."""
    d = _get(f"{cfg.BINANCE_SPOT_REST}/ticker/bookTicker", {"symbol": "USDCUSDT"}, cfg.USER_AGENT)
    b, a = _f(d.get("bidPrice")), _f(d.get("askPrice"))
    return (b + a) / 2 if b and a else None


async def run(cfg, md, log, stop):
    c = contract(cfg)
    sym = cfg.BINANCE_SYMBOL
    # combined stream: messages arrive wrapped as {"stream": ..., "data": {...}}
    base = c["ws"].rsplit("/ws", 1)[0]
    url = f"{base}/stream?streams={sym.lower()}@bookTicker/{sym.lower()}@depth20@100ms"

    async def on_open(ws):
        pass                                    # the stream names in the URL are the subscription

    def on_message(msg):
        msg = msg.get("data", msg)
        q = parse_book_ticker(msg, sym, c["qty_unit"])
        if q:
            md.put(NAME, *q)
            return
        d = parse_depth(msg, sym, c["qty_unit"])
        if d:
            md.put_depth(NAME, *d)
        elif msg.get("code") or msg.get("error"):
            md.binance.last_error = json.dumps(msg)[:300]
            log.warning(f"binance: server error {json.dumps(msg)[:300]}")

    await run_socket(NAME, url, md, log, on_open, on_message, cfg.STALE_SOCKET_S, stop)
