"""
core/api.py — the Delta Exchange India REST surface this strategy uses.
=======================================================================

Public market data only. Every endpoint below is unauthenticated, which is what
lets the whole paper build run before an API key exists.

Base URL is India production. Delta also runs a testnet at
`https://cdn-ind.testnet.deltaex.org` with the SAME paths — swap BASE and every
call here works against it, which is how the live order layer will be rehearsed
when we get to it.

Why one module instead of `requests` calls scattered around: rate limits are per
account and shared across everything we run (20,000 units / 5 min, a read costing
3). The collector, the engine and the tools all go through here so there is one
place to see — and one place to fix — how hard we are hitting the exchange.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import time
import json
import urllib.parse
import urllib.request

BASE = "https://api.india.delta.exchange"
TESTNET = "https://cdn-ind.testnet.deltaex.org"

# Delta rejects requests without a User-Agent, and uses it to identify clients in
# their logs when something goes wrong — so it names this system, not "python".
_UA = "fyers-pipeline/delta-btc-strangle"
_TIMEOUT = 30


class DeltaError(RuntimeError):
    pass


def _get(path: str, params: dict = None, base: str = BASE, retries: int = 3) -> dict:
    """One GET, with a bounded retry. Raises DeltaError rather than returning a
    half-answer — a caller that silently treats a failed fetch as "no contracts"
    would look exactly like an empty chain, and an empty chain is a decision the
    strategy is allowed to act on."""
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json", "User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                d = json.loads(r.read().decode())
            if not d.get("success", True):
                raise DeltaError(f"{path}: {d.get('error')}")
            return d
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise DeltaError(f"GET {url} failed after {retries}: {last}")


# ── products (the contract master) ───────────────────────────────────────
def products(contract_types: str = "call_options,put_options",
             states: str = "live", base: str = BASE) -> list[dict]:
    """Every listed contract of the given types, following pagination.

    This is the equivalent of Kite's instrument dump: strikes, product_ids, tick
    sizes and contract values all come from here and are never hand-built, so the
    thing we price is by construction the thing we would trade."""
    out, after = [], None
    while True:
        p = {"contract_types": contract_types, "states": states, "page_size": 500}
        if after:
            p["after"] = after
        d = _get("/v2/products", p, base=base)
        rows = d.get("result") or []
        out += rows
        after = (d.get("meta") or {}).get("after")
        if not after or not rows:
            break
    return out


def btc_options(base: str = BASE) -> list[dict]:
    """Live BTC call+put contracts only."""
    return [r for r in products(base=base)
            if r.get("contract_unit_currency") == "BTC"]


# ── tickers (the whole chain in one call) ────────────────────────────────
def tickers(contract_types: str = "call_options,put_options",
            base: str = BASE) -> list[dict]:
    return _get("/v2/tickers", {"contract_types": contract_types}, base=base)["result"]


def btc_option_tickers(base: str = BASE) -> list[dict]:
    """One call returns the ENTIRE BTC option chain with mark, best bid/ask and
    sizes, IV, full greeks, OI and the spot index. Everything the strategy and the
    archive need, at a cost of 3 rate-limit units — which is why the collector can
    afford to run every minute forever."""
    return [t for t in tickers(base=base)
            if t.get("underlying_asset_symbol") == "BTC"]


# ── depth ────────────────────────────────────────────────────────────────
def orderbook(symbol: str, base: str = BASE) -> dict:
    """L2 book for one contract: {'buy': [...], 'sell': [...]}.

    Used to price a marketable order against real levels instead of a multiple of
    a stale mark — the lesson from the 26-Aug depth-pricing fix on the NSE side.
    """
    return _get(f"/v2/l2orderbook/{symbol}", base=base)["result"]


# ── candles ──────────────────────────────────────────────────────────────
def candles(symbol: str, resolution: str, start: int, end: int,
            base: str = BASE) -> list[dict]:
    """OHLCV history, newest first.

    ⚠️ Works for LIVE symbols only. An EXPIRED option returns an empty list — the
    same trap the Fyers options pipeline hit. That single fact is why this package
    ships a chain collector: history for an expired contract cannot be fetched
    later at any price, so it has to be captured as it happens.
    """
    return _get("/v2/history/candles",
                {"symbol": symbol, "resolution": resolution,
                 "start": int(start), "end": int(end)}, base=base)["result"] or []
