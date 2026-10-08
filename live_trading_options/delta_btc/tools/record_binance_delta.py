"""
tools/record_binance_delta.py — full-chain recorder, Binance vs Delta BTC options.
=================================================================================

Every 60 s, read-only public endpoints, no keys:

  TICKERS  (every strike, every expiry, both exchanges — one call each)
      bid / ask (+ sizes where the exchange gives them), last, mark, IV, greeks,
      24h volume (BTC), Binance premium turnover + trade count, Delta notional turnover,
      open interest (BTC + USD)
      -> data/liquidity_compare/{date}/tick_{HHMMSS}.parquet

  ORDER BOOKS (20 levels each side, sizes in BTC on both — Delta contracts x 0.001)
      nearest 2 expiries: every minute
      every other expiry: rotating slice, each contract about every ROTATE_MIN minutes
      -> data/liquidity_compare/{date}/book_{HHMMSS}.parquet
         columns: best bid/ask, sizes, total depth, avg price to sell/buy 1 BTC,
                  and the raw levels (bid_px, bid_qty, ask_px, ask_qty lists)

  OPEN INTEREST (Binance, per expiry) every 15 min — Delta's OI is in its ticker.

The order-book load is tiered on purpose: the VPS also trades on Delta, and the full
chain's books every minute (~1,000+ calls) could get that IP rate-limited.

Expiry times differ: Binance 08:00 UTC (13:30 IST), Delta 12:00 UTC (17:30 IST).

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\tools\\record_binance_delta.py
Stop: kill the PID in data/liquidity_compare/recorder.pid
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import time
import socket
import traceback
import datetime as dt
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "data" / "liquidity_compare"
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "recorder.log"
BE = "https://eapi.binance.com/eapi/v1"
DE = "https://api.india.delta.exchange/v2"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INTERVAL = 60
NEAR_EXPIRIES = 2
ROTATE_MIN = 10
LEVELS = 20
WORKERS = 4
LOCK_PORT = 47662
BINANCE_WEIGHT_CAP = 300
S = requests.Session()


def log(msg):
    line = f"{dt.datetime.now(IST):%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def get(url, params=None):
    for i in range(3):
        try:
            r = S.get(url, params=params, timeout=15)
            if r.status_code in (418, 429):
                wait = float(r.headers.get("Retry-After") or 5 + 5 * i)
                log(f"rate limited {r.status_code} on {url.split('/')[-1]}, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            used = r.headers.get("x-mbx-used-weight-1m")
            if used and int(used) > BINANCE_WEIGHT_CAP:
                # stay well under Binance's per-minute weight: wait for the next minute
                time.sleep(61 - dt.datetime.now().second)
            return r.json()
        except requests.HTTPError:
            raise
        except Exception:
            if i == 2:
                raise
            time.sleep(1 + i)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def walk(levels, qty=1.0):
    got = cost = 0.0
    for p, s in levels:
        take = min(s, qty - got)
        got += take
        cost += take * p
        if got >= qty - 1e-9:
            return cost / got
    return None


def book_row(bids, asks):
    return {"bid": bids[0][0] if bids else None, "ask": asks[0][0] if asks else None,
            "bid_size": bids[0][1] if bids else None, "ask_size": asks[0][1] if asks else None,
            "bid_depth": sum(q for _, q in bids), "ask_depth": sum(q for _, q in asks),
            "bid_levels": len(bids), "ask_levels": len(asks),
            "sell_1btc": walk(bids), "buy_1btc": walk(asks),
            "bid_px": [p for p, _ in bids], "bid_qty": [q for _, q in bids],
            "ask_px": [p for p, _ in asks], "ask_qty": [q for _, q in asks]}


# ── Binance ──────────────────────────────────────────────────────────────
def binance_chain():
    info = get(BE + "/exchangeInfo")
    expiry = {s["symbol"]: dt.datetime.fromtimestamp(s["expiryDate"] / 1000, dt.timezone.utc)
              for s in info["optionSymbols"] if s["underlying"] == "BTCUSDT"}
    tick = {t["symbol"]: t for t in get(BE + "/ticker") if t["symbol"] in expiry}
    mark = {m["symbol"]: m for m in get(BE + "/mark") if m["symbol"] in expiry}
    rows = []
    for sym, e in expiry.items():
        t, m = tick.get(sym, {}), mark.get(sym, {})
        _, _, k, cp = sym.split("-")
        rows.append({"exchange": "binance", "symbol": sym, "expiry": e, "strike": float(k),
                     "opt_type": "CE" if cp == "C" else "PE",
                     "bid": f(t.get("bidPrice")), "ask": f(t.get("askPrice")),
                     "bid_size": None, "ask_size": None,
                     "last": f(t.get("lastPrice")), "mark": f(m.get("markPrice")),
                     "mark_iv": f(m.get("markIV")), "bid_iv": f(m.get("bidIV")), "ask_iv": f(m.get("askIV")),
                     "delta": f(m.get("delta")), "gamma": f(m.get("gamma")),
                     "theta": f(m.get("theta")), "vega": f(m.get("vega")),
                     "volume_24h_btc": f(t.get("volume")), "volume_24h_contracts": None,
                     "turnover_24h_usd": f(t.get("amount")), "notional_24h_usd": None,
                     "trades_24h": f(t.get("tradeCount")), "oi_btc": None, "oi_usd": None,
                     "high_24h": f(t.get("high")), "low_24h": f(t.get("low"))})
    return rows, expiry


def binance_book(sym):
    b = get(BE + "/depth", {"symbol": sym, "limit": LEVELS})
    return ([(float(p), float(q)) for p, q in b.get("bids", [])][:LEVELS],
            [(float(p), float(q)) for p, q in b.get("asks", [])][:LEVELS])


def binance_oi(expiries):
    rows = []
    for e in sorted({e.strftime("%y%m%d") for e in expiries}):
        try:
            for x in get(BE + "/openInterest", {"underlyingAsset": "BTC", "expiration": e}):
                rows.append({"symbol": x["symbol"], "oi_btc": f(x.get("sumOpenInterest")),
                             "oi_usd": f(x.get("sumOpenInterestUsd"))})
        except Exception as ex:
            log(f"binance OI {e} failed: {type(ex).__name__}: {ex}")
    return rows


# ── Delta ────────────────────────────────────────────────────────────────
def delta_chain():
    tick = get(DE + "/tickers", {"contract_types": "call_options,put_options",
                                 "underlying_asset_symbols": "BTC"})["result"]
    rows, expiry = [], {}
    for t in tick:
        try:
            cp, _, k, code = t["symbol"].split("-")
            e = dt.datetime.strptime(code, "%d%m%y").replace(hour=12, tzinfo=dt.timezone.utc)
        except Exception:
            continue
        expiry[t["symbol"]] = e
        q, g = t.get("quotes") or {}, t.get("greeks") or {}
        cv = 0.001   # book / quote sizes are contracts
        rows.append({"exchange": "delta", "symbol": t["symbol"], "expiry": e, "strike": float(k),
                     "opt_type": "CE" if cp == "C" else "PE",
                     "bid": f(q.get("best_bid")), "ask": f(q.get("best_ask")),
                     "bid_size": (f(q.get("bid_size")) or 0) * cv if q.get("bid_size") is not None else None,
                     "ask_size": (f(q.get("ask_size")) or 0) * cv if q.get("ask_size") is not None else None,
                     "last": f(t.get("close")), "mark": f(t.get("mark_price")),
                     "mark_iv": f(q.get("mark_iv")), "bid_iv": f(q.get("bid_iv")), "ask_iv": f(q.get("ask_iv")),
                     "delta": f(g.get("delta")), "gamma": f(g.get("gamma")),
                     "theta": f(g.get("theta")), "vega": f(g.get("vega")),
                     # Delta's ticker volume / oi are already in BTC (size = contracts)
                     "volume_24h_btc": f(t.get("volume")), "volume_24h_contracts": f(t.get("size")),
                     "turnover_24h_usd": None, "notional_24h_usd": f(t.get("turnover_usd")),
                     "trades_24h": None,
                     "oi_btc": f(t.get("oi")), "oi_usd": f(t.get("oi_value_usd")),
                     "high_24h": f(t.get("high")), "low_24h": f(t.get("low"))})
    return rows, expiry


def delta_book(sym):
    b = get(DE + f"/l2orderbook/{sym}", {"depth": LEVELS})["result"]
    return ([(float(x["price"]), float(x["size"]) * 0.001) for x in b.get("buy", [])][:LEVELS],
            [(float(x["price"]), float(x["size"]) * 0.001) for x in b.get("sell", [])][:LEVELS])


# ── one minute ───────────────────────────────────────────────────────────
STATE = {"n": 0, "oi": {}}


def pick_books(expiry, n):
    now = dt.datetime.now(dt.timezone.utc)
    live = {s: e for s, e in expiry.items() if e > now}
    near_exp = sorted(set(live.values()))[:NEAR_EXPIRIES]
    near = [s for s, e in live.items() if e in near_exp]
    far = sorted(s for s, e in live.items() if e not in near_exp)
    if far:
        size = -(-len(far) // ROTATE_MIN)
        i = n % ROTATE_MIN
        far = far[i * size:(i + 1) * size]
    return near, far


def snapshot():
    now = dt.datetime.now(IST)
    stamp = now.replace(tzinfo=None, microsecond=0)
    spot = f(get(BE + "/index", {"underlying": "BTCUSDT"}).get("indexPrice"))
    day = OUT / now.strftime("%Y-%m-%d")
    day.mkdir(exist_ok=True)
    ticks, books = [], []
    for name, chain, book in (("binance", binance_chain, binance_book), ("delta", delta_chain, delta_book)):
        try:
            rows, expiry = chain()
            if name == "binance" and STATE["n"] % 15 == 0:
                STATE["oi"] = {r["symbol"]: r for r in binance_oi(set(expiry.values()))}
            if name == "binance":
                for r in rows:
                    o = STATE["oi"].get(r["symbol"])
                    if o:
                        r["oi_btc"], r["oi_usd"] = o["oi_btc"], o["oi_usd"]
            ticks += rows
            near, far = pick_books(expiry, STATE["n"])

            def one(sym, tier):
                try:
                    bids, asks = book(sym)
                    return {"exchange": name, "symbol": sym, "expiry": expiry[sym], "tier": tier,
                            **book_row(bids, asks)}
                except Exception as ex:
                    return {"exchange": name, "symbol": sym, "expiry": expiry[sym], "tier": tier,
                            "error": f"{type(ex).__name__}: {ex}"[:200]}
            with ThreadPoolExecutor(WORKERS) as ex:
                books += list(ex.map(lambda s: one(s, "near"), near))
                books += list(ex.map(lambda s: one(s, "far"), far))
        except Exception as e:
            log(f"{name} failed: {type(e).__name__}: {e}")
    for rows, prefix in ((ticks, "tick"), (books, "book")):
        if rows:
            df = pd.DataFrame(rows)
            df["time_ist"] = stamp
            df["spot"] = spot
            df.to_parquet(day / f"{prefix}_{now:%H%M%S}.parquet", index=False)
    STATE["n"] += 1
    errs = sum(1 for b in books if b.get("error"))
    return len(ticks), len(books), errs


def main():
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)          # listening, so `-Status` (Get-NetTCPConnection) can see it
    except OSError:
        print("another recorder is already running")
        return
    (OUT / "recorder.pid").write_text(str(os.getpid()))
    log(f"recorder start pid {os.getpid()}")
    while True:
        t0 = time.time()
        try:
            t, b, e = snapshot()
            if STATE["n"] % 30 == 1 or e:
                log(f"snapshot {STATE['n']}: {t} tickers, {b} books, {e} book errors, "
                    f"{time.time() - t0:.0f}s")
        except Exception:
            log("snapshot error\n" + traceback.format_exc())
        time.sleep(max(1, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
