"""
strangle_system/data/spot_backfill.py
======================================
Backfill DAILY spot OHLC for an underlying's index, for L1 realized vol.

Needed because some underlyings (SENSEX/BSE) have no 5-min history in the main
data tree. Daily bars are sufficient for Garman-Klass / Yang-Zhang / close-to-
close RV. This is INDEX history (free via Fyers history, resolution=D), not the
option-chain data — unrelated to the forward-accumulated snapshots.

Fyers caps daily-resolution history at ~366 days/call, so we chunk by year.

    python -m strangle_system.data.spot_backfill                 # all ACTIVE_UNDERLYINGS
    python -m strangle_system.data.spot_backfill --underlyings SENSEX --years 3
"""

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system import config
from auth.fyers_auth import get_fyers_client

config.reconfigure_stdout()


def spot_daily_path(underlying: str) -> Path:
    config.SPOT_DAILY_DIR.mkdir(parents=True, exist_ok=True)
    return config.SPOT_DAILY_DIR / f"{underlying}.parquet"


def _fetch_chunk(fy, symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    r = fy.history(data={"symbol": symbol, "resolution": "D", "date_format": "1",
                         "range_from": str(start), "range_to": str(end), "cont_flag": "1"})
    if r.get("s") != "ok":
        print(f"  {start}->{end}: {r.get('message')}")
        return None
    candles = r.get("candles", [])
    if not candles:
        return None
    df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
    df["datetime"] = (pd.to_datetime(df["epoch"], unit="s")
                      .dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
    return df.drop(columns="epoch").set_index("datetime")


def backfill(underlying: str, years: int = 3, fy=None) -> Optional[pd.DataFrame]:
    cfg = config.UNDERLYINGS.get(underlying)
    if not cfg:
        print(f"Unknown underlying: {underlying}")
        return None
    symbol = cfg["index_symbol"]
    fy = fy or get_fyers_client()

    end = date.today()
    start_all = end - timedelta(days=365 * years)
    print(f"\n{underlying} ({symbol}) daily spot {start_all} → {end}")

    frames, a = [], start_all
    while a < end:
        b = min(a + timedelta(days=360), end)
        chunk = _fetch_chunk(fy, symbol, a, b)
        if chunk is not None:
            frames.append(chunk)
        a = b + timedelta(days=1)
        time.sleep(config.SLEEP_BETWEEN_CALLS)

    if not frames:
        print(f"  No data for {underlying}.")
        return None

    df = (pd.concat(frames).sort_index()
          [["open", "high", "low", "close", "volume"]])
    df = df[~df.index.duplicated(keep="last")]
    path = spot_daily_path(underlying)
    df.to_parquet(path, compression="snappy")
    print(f"  Saved {len(df)} daily bars → {path}  ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def load_spot_daily(underlying: str) -> Optional[pd.DataFrame]:
    """Load backfilled daily OHLC (DatetimeIndex). None if absent."""
    path = spot_daily_path(underlying)
    if not path.exists():
        return None
    return pd.read_parquet(path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Backfill daily spot OHLC for L1 realized vol")
    ap.add_argument("--underlyings", nargs="+", default=None)
    ap.add_argument("--years", type=int, default=3)
    args = ap.parse_args()
    fy = get_fyers_client()
    for u in (args.underlyings or config.ACTIVE_UNDERLYINGS):
        backfill(u, years=args.years, fy=fy)
