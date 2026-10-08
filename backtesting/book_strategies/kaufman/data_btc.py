import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC bar loader for the Kaufman harness
=========================================
Binance BTCUSDT 1-minute archive (crypto/download_binance_btc.py) resampled to
any timeframe and cached.

BTC trades 24/7, so there is no session boundary to fake: daily/weekly bars
cut at 00:00 UTC (the convention every crypto venue and chart uses), and no
"Sunday stub" bars exist to clean up. Weekly bars start Monday.

Unlike the Dukascopy set, VOLUME is real traded volume (BTC), so volume-based
strategies are testable here.

Also exposes the Delta Exchange BTCUSD perp (1h) for the venue cross-check.
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT   = Path(__file__).parents[3]
ARCHIVE   = PROJECT / "data" / "CRYPTO_BTCUSDT"
CACHE_DIR = Path(__file__).parent / "_cache_btc"
CACHE_DIR.mkdir(exist_ok=True)

_AGG = dict(open=("open", "first"), high=("high", "max"),
            low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))

# Delta Exchange India: taker 0.05% per side, plus ~0.01% slippage on a
# market order in BTC perp. Fraction of notional, per side -> harness charges
# it on entry and exit value.
DELTA_COST_PCT = 0.0006


def _base_1m() -> pd.DataFrame:
    f = CACHE_DIR / "btcusdt_1min_full.parquet"
    files = sorted(ARCHIVE.rglob("ohlcv_1m_*.parquet"))
    if not files:
        raise FileNotFoundError(f"No BTC data in {ARCHIVE} — run crypto/download_binance_btc.py")
    newest = max(p.stat().st_mtime for p in files)
    if f.exists() and f.stat().st_mtime >= newest:
        return pd.read_parquet(f)

    df = pd.concat([pd.read_parquet(x) for x in files], ignore_index=True)
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df.to_parquet(f)
    # Any resampled cache is now stale.
    for old in CACHE_DIR.glob("btcusdt_*.parquet"):
        if old != f:
            old.unlink()
    logger.info(f"BTC: cached {len(df):,} 1-minute bars")
    return df


def get_bars(timeframe: str, start: str = None, end: str = None) -> pd.DataFrame:
    """Resampled OHLCV bars, optionally date-filtered (UTC)."""
    base_file = CACHE_DIR / "btcusdt_1min_full.parquet"
    f = CACHE_DIR / f"btcusdt_{timeframe}.parquet"

    if f.exists() and base_file.exists() and f.stat().st_mtime >= base_file.stat().st_mtime:
        df = pd.read_parquet(f)
    else:
        base = _base_1m()
        if timeframe == "1min":
            df = base
        else:
            rule = "W-MON" if timeframe in ("1W", "W") else timeframe
            kw = dict(closed="left", label="left")
            df = base.resample(rule, **kw).agg(**_AGG).dropna(subset=["open", "close"])
        df.to_parquet(f)

    if start:
        df = df[df.index >= start]
    if end:
        df = df[df.index < end]
    return df


def delta_perp_1h() -> pd.DataFrame:
    df = pd.read_parquet(ARCHIVE / "delta_btcusd_1h.parquet")
    return df.set_index("datetime").sort_index()


def coverage() -> tuple:
    df = _base_1m()
    return df.index.min(), df.index.max(), len(df)
