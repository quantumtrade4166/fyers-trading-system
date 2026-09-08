"""
tools/collect_chain.py — the BTC option-chain archive. RUN THIS FIRST, FOREVER.
==============================================================================

Delta serves candle history for LIVE contracts only. Ask for an EXPIRED option's
candles and you get an empty list — verified, not assumed. Since every daily
option this strategy trades is expired within 24 hours, **there is no way to
backtest it on data we did not capture ourselves.** Every minute this is not
running is a minute of history that cannot be bought back later.

So: one call a minute to `/v2/tickers` returns the WHOLE BTC chain — mark, best
bid/ask with sizes, mark/bid/ask IV, the full greeks, OI and the spot index. That
is 3 rate-limit units out of 20,000 per 5 minutes, which is why this can simply
run forever.

Written as immutable chunk files, never appended in place:

    data/chain_archive/2026-09-04/chunk_141500.parquet

A crash or a kill can therefore lose at most one un-flushed buffer (5 minutes),
and can never corrupt an already-written chunk. `compact_archive.py` merges a
finished day into one file afterwards.

Run:
    .venv/Scripts/python.exe live_trading_options/delta_btc/tools/collect_chain.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import time
import datetime as dt
import traceback

import pandas as pd

from core.api import btc_option_tickers, DeltaError
from core.shared import singleton, PORT_BTC_COLLECTOR, ARCHIVE, LOGS, now_ist

POLL_SECONDS = 60
FLUSH_SECONDS = 300
LOG = LOGS / "collect_chain.log"


def log(msg: str):
    # IST, deliberately: this machine's clock is GMT+10, so a local timestamp
    # would put every archive line ~4.5h away from the IST the data is keyed on.
    line = f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _f(v):
    """Delta returns most numerics as STRINGS, and null for anything unquoted.
    Everything numeric goes through here so the parquet columns come out as real
    floats instead of a mix of str and None that pandas would type as object."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def flatten(t: dict, captured: dt.datetime) -> dict:
    """One ticker -> one archive row.

    `symbol` is Delta's own contract id (C-BTC-82000-040926) and carries the side,
    strike and expiry, but those are split into their own columns too — a backtest
    filtering on strike should never have to parse a string.
    """
    q = t.get("quotes") or {}
    g = t.get("greeks") or {}
    sym = t.get("symbol", "")
    parts = sym.split("-")
    return {
        "captured": captured,
        "symbol": sym,
        "product_id": t.get("product_id"),
        "opt_type": "CE" if sym.startswith("C-") else "PE",
        "strike": _f(t.get("strike_price")),
        "expiry": parts[-1] if len(parts) == 4 else None,
        "spot": _f(t.get("spot_price")),
        "mark": _f(t.get("mark_price")),
        "best_bid": _f(q.get("best_bid")),
        "best_ask": _f(q.get("best_ask")),
        "bid_size": _f(q.get("bid_size")),
        "ask_size": _f(q.get("ask_size")),
        "mark_iv": _f(q.get("mark_iv")),
        "bid_iv": _f(q.get("bid_iv")),
        "ask_iv": _f(q.get("ask_iv")),
        "delta": _f(g.get("delta")),
        "gamma": _f(g.get("gamma")),
        "theta": _f(g.get("theta")),
        "vega": _f(g.get("vega")),
        "oi_contracts": _f(t.get("oi_contracts")),
        "oi_value_usd": _f(t.get("oi_value_usd")),
        "volume": _f(t.get("volume")),
        "turnover_usd": _f(t.get("turnover_usd")),
        "close": _f(t.get("close")),
        "contract_value": _f(t.get("contract_value")),
    }


def flush(buf: list[dict]) -> int:
    """Write the buffer out, splitting by capture DATE so a buffer that straddles
    midnight lands in the right day's folder rather than smearing across it."""
    if not buf:
        return 0
    df = pd.DataFrame(buf)
    written = 0
    for day, part in df.groupby(df["captured"].dt.date):
        d = ARCHIVE / str(day)
        d.mkdir(parents=True, exist_ok=True)
        stamp = part["captured"].max().strftime("%H%M%S")
        path = d / f"chunk_{stamp}.parquet"
        # A same-second collision can only happen on a restart inside the same
        # second; suffix rather than overwrite, because an existing chunk is
        # already-captured history and must never be lost.
        n = 1
        while path.exists():
            path = d / f"chunk_{stamp}_{n}.parquet"
            n += 1
        part.to_parquet(path, index=False)
        written += len(part)
        log(f"  flushed {len(part):>6} rows -> {path.relative_to(ARCHIVE)}")
    return written


def main():
    if not singleton.acquire(PORT_BTC_COLLECTOR):
        log("another collector holds the lock — this duplicate exits.")
        return

    log(f"collector start — poll {POLL_SECONDS}s, flush {FLUSH_SECONDS}s, "
        f"archive {ARCHIVE}")
    buf: list[dict] = []
    last_flush = time.monotonic()
    polls = fails = total = 0

    while True:
        started = time.monotonic()
        try:
            rows = btc_option_tickers()
            captured = now_ist().replace(microsecond=0)
            buf += [flatten(t, captured) for t in rows]
            polls += 1
            if polls % 15 == 0:
                spot = next((_f(t.get("spot_price")) for t in rows
                             if t.get("spot_price")), None)
                log(f"  poll {polls}: {len(rows)} contracts, spot {spot}, "
                    f"buffered {len(buf)}, archived {total}, fails {fails}")
        except Exception as e:
            fails += 1
            # A failed poll is a gap in the archive, never a reason to stop: the
            # next minute's data is still worth having.
            log(f"  poll failed ({fails}): {type(e).__name__}: {e}")

        if time.monotonic() - last_flush >= FLUSH_SECONDS:
            try:
                total += flush(buf)
                buf = []
            except Exception as e:
                # Keep the buffer on a write failure so the next flush retries it.
                log(f"  FLUSH FAILED — buffer retained ({len(buf)} rows): {e}")
                log(traceback.format_exc())
            last_flush = time.monotonic()

        time.sleep(max(0.0, POLL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted — exiting.")
