import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Cached multi-timeframe XAUUSD bar loader for the Kaufman backtests.

Loads the 1-second archive ONCE into a cached 1-minute file, then derives
every higher timeframe from that by resampling — far faster than re-reading
the 1s archive per timeframe (~90s each).
"""

import logging
from pathlib import Path
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))
from forex.xauusd_data_loader import load_xauusd, resample_ohlc

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "_cache"
CACHE_DIR.mkdir(exist_ok=True)

TEST_YEARS = 7
END_DATE   = date.today()
START_DATE = END_DATE - timedelta(days=365 * TEST_YEARS)

BASE_TF = "1min"

# Spot gold runs ~24×5, opening Sunday ~22:00 UTC. Resampling to calendar
# days therefore produces ~350 stub "Sunday" bars of only 28-200 minutes,
# which distort ATR and every OHLC pattern. Daily/weekly bars are instead
# cut on the 22:00 UTC forex session boundary so the Sunday open merges
# into Monday — matching how gold is actually quoted daily.
SESSION_OFFSET = "22h"
_OFFSET_TFS = {"1D", "1d", "D", "1W", "W", "12h", "8h"}


def _base_bars() -> pd.DataFrame:
    """The cached 1-minute base series everything else is derived from."""
    f = CACHE_DIR / f"xauusd_{BASE_TF}_{TEST_YEARS}y.parquet"
    if f.exists():
        return pd.read_parquet(f)
    logger.info(f"Building {BASE_TF} base cache from the 1s archive (one-off, ~2 min)...")
    df = load_xauusd(str(START_DATE), str(END_DATE), timeframe=BASE_TF)
    df.to_parquet(f)
    return df


def get_bars_range(start: str, end: str, timeframe: str, tag: str) -> pd.DataFrame:
    """
    Bars for an arbitrary date range — used for out-of-sample testing on the
    2003-2019 history that no strategy has been fitted to.

    `tag` names the cache file (e.g. "oos", "full").
    """
    f = CACHE_DIR / f"xauusd_{timeframe}_{tag}.parquet"
    if f.exists():
        return pd.read_parquet(f)

    base_f = CACHE_DIR / f"xauusd_{BASE_TF}_{tag}.parquet"
    if base_f.exists():
        base = pd.read_parquet(base_f)
    else:
        logger.info(f"Building {BASE_TF} cache for {tag} ({start} -> {end})...")
        base = load_xauusd(start, end, timeframe=BASE_TF)
        base.to_parquet(base_f)

    if timeframe == BASE_TF:
        return base

    if timeframe in _OFFSET_TFS:
        df = base.resample(timeframe, closed="left", label="left",
                           offset=SESSION_OFFSET).agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"),     close=("close", "last"),
        ).dropna(subset=["open", "close"])
    else:
        df = resample_ohlc(base, timeframe)

    df.to_parquet(f)
    logger.info(f"Cached {tag} {timeframe}: {len(df):,} bars")
    return df


def get_bars(timeframe: str) -> pd.DataFrame:
    """Return cached OHLC bars at `timeframe` (pandas offset alias)."""
    if timeframe == BASE_TF:
        return _base_bars()

    f = CACHE_DIR / f"xauusd_{timeframe}_{TEST_YEARS}y.parquet"
    if f.exists():
        return pd.read_parquet(f)

    base = _base_bars()

    if timeframe in _OFFSET_TFS:
        df = base.resample(timeframe, closed="left", label="left",
                           offset=SESSION_OFFSET).agg(
            open=("open", "first"), high=("high", "max"),
            low=("low", "min"),     close=("close", "last"),
        ).dropna(subset=["open", "close"])
    else:
        df = resample_ohlc(base, timeframe)

    df.to_parquet(f)
    logger.info(f"Cached {timeframe}: {len(df):,} bars")
    return df
