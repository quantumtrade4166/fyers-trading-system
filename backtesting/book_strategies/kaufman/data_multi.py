import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Multi-instrument bar loader for the Dukascopy 1-minute basket
================================================================
Reads H:\\My Drive\\Dukascopy_1m\\{instrument}\\{year}\\ohlcv_1m_{MM}.parquet,
resamples to any timeframe, and caches the result locally.

Handles the 22:00 UTC session boundary for daily/weekly bars — without it,
24x5 instruments produce ~350 stub "Sunday" bars of a few minutes each that
wreck ATR and every OHLC pattern.
"""

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

ARCHIVE   = Path("H:/My Drive/Dukascopy_1m")
CACHE_DIR = Path(__file__).parent / "_cache_multi"
CACHE_DIR.mkdir(exist_ok=True)

SESSION_OFFSET = "22h"
_OFFSET_TFS = {"1D", "1d", "D", "1W", "W", "12h", "8h"}

# Round-trip cost in PRICE UNITS per unit of exposure (spread + slippage +
# commission). Index CFD spreads are tight relative to price; these are
# deliberately a little conservative versus typical broker quotes.
COSTS = {
    "usa500idxusd":  0.60,    # S&P 500  ~5,000 pts  -> ~0.012% of price
    "usatechidxusd": 2.00,    # Nasdaq   ~20,000 pts -> ~0.010%
    "lightcmdusd":   0.030,   # WTI      ~$70        -> ~0.043%
    "brentcmdusd":   0.030,
    "xagusd":        0.020,   # Silver   ~$30        -> ~0.067%
    "eurusd":        0.00012, # ~1.2 pips
    "gbpusd":        0.00015,
    "usdjpy":        0.015,
    "audusd":        0.00015,
    "usdchf":        0.00015,
    "usdcad":        0.00018,
    "xauusd":        0.35,    # gold, matches earlier work
}

LABELS = {
    "usa500idxusd": "S&P 500", "usatechidxusd": "Nasdaq 100",
    "lightcmdusd": "WTI Crude", "brentcmdusd": "Brent Crude",
    "xagusd": "Silver", "eurusd": "EUR/USD", "gbpusd": "GBP/USD",
    "usdjpy": "USD/JPY", "audusd": "AUD/USD", "usdchf": "USD/CHF",
    "usdcad": "USD/CAD", "xauusd": "Gold",
}


def _base_1m(inst: str) -> pd.DataFrame:
    f = CACHE_DIR / f"{inst}_1min_full.parquet"
    if f.exists():
        return pd.read_parquet(f)

    root = ARCHIVE / inst
    files = sorted(root.rglob("ohlcv_1m_*.parquet"))
    if not files:
        raise FileNotFoundError(f"No data for {inst} in {root}")

    df = pd.concat([pd.read_parquet(x) for x in files], ignore_index=True)
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df.to_parquet(f)
    logger.info(f"{inst}: cached {len(df):,} 1-minute bars")
    return df


def get_bars(inst: str, timeframe: str, start: str = None, end: str = None) -> pd.DataFrame:
    """Resampled OHLC bars for one instrument, optionally date-filtered."""
    tag = f"{inst}_{timeframe}"
    f = CACHE_DIR / f"{tag}.parquet"

    if f.exists():
        df = pd.read_parquet(f)
    else:
        base = _base_1m(inst)
        if timeframe == "1min":
            df = base
        elif timeframe in _OFFSET_TFS:
            df = base.resample(timeframe, closed="left", label="left",
                               offset=SESSION_OFFSET).agg(
                open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
            ).dropna(subset=["open", "close"])
        else:
            df = base.resample(timeframe, closed="left", label="left").agg(
                open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
            ).dropna(subset=["open", "close"])
        df.to_parquet(f)

    if start:
        df = df[df.index >= start]
    if end:
        df = df[df.index <= end]
    return df


def available() -> list[str]:
    if not ARCHIVE.exists():
        return []
    return sorted(d.name for d in ARCHIVE.iterdir()
                  if d.is_dir() and any(d.rglob("ohlcv_1m_*.parquet")))


def coverage(inst: str) -> tuple:
    df = _base_1m(inst)
    return df.index.min(), df.index.max(), len(df)
