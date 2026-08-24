"""Indian-market bar loader for the Kaufman catalog.

The counterpart to data_multi.py (Dukascopy/forex). Same contract — get_bars()
returns an OHLC frame on a datetime index — so the 78 strategies and the harness
need no changes to run on NSE data.

Three differences from the forex loader, all of them consequences of the market
rather than preferences:

1. **No session offset.** data_multi carries a 22:00 UTC boundary because a 24x5
   instrument otherwise produces stub "Sunday" bars that wreck ATR and every
   OHLC pattern. NSE trades 09:15-15:30 in a single calendar day, so a daily bar
   is unambiguous and no offset is needed.

2. **Costs are PERCENTAGES, not price units.** data_multi's COSTS are absolute
   (0.35 USD per ounce of gold) because each instrument has one price scale.
   That cannot work across 500 stocks spanning Rs 50 to Rs 40,000 — a flat rupee
   cost would be a rounding error on one and a wall on the other. Indian charges
   are levied on turnover anyway, so percent is also the more faithful model.

3. **Sizing must respect lots.** Handled in the harness, not here.

Instruments:
    NIFTY       index, 1-min derived from the options chain's spot column,
                2021-2026 (see build_nifty_spot.py for the accuracy audit)
    NIFTY_REAL  index, real 5-min bars, 2024-05 onward — shorter but true wicks
    BANKNIFTY   index, real 5-min bars, 2024-05 onward
    <SYMBOL>    any Nifty-500 constituent, daily, 2005-2026 (e.g. "RELIANCE")
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INDEX_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY50_INDEX"
BANKNIFTY_DIR = PROJECT_ROOT / "data" / "NSE_NIFTYBANK_INDEX"
EQUITY_5MIN_DIR = PROJECT_ROOT / "data"
DAILY_DIR = PROJECT_ROOT / "Nifty 500 Daily Data"
CACHE_DIR = Path(__file__).parent / "_cache_nifty"
CACHE_DIR.mkdir(exist_ok=True)

SESSION_START = "09:15"
SESSION_END = "15:30"

# ---------------------------------------------------------------------------
# Cost model — ROUND-TRIP, as a fraction of turnover.
#
# Built from the published Indian charge structure (rates as of 2026). Each is
# the sum of: brokerage, STT, exchange transaction charge, SEBI turnover fee,
# GST at 18% on (brokerage + txn charges), and stamp duty on the buy side.
#
# These are the *structural* charges only. Slippage is deliberately NOT baked
# in — it is the dominant and most uncertain cost, so it is swept separately
# (see SLIPPAGE_SWEEP) rather than hidden inside a single number.
#
# Sources of asymmetry worth remembering: STT on equity intraday is charged on
# the SELL side at 0.025%, on delivery at 0.1% BOTH sides, and on index futures
# at 0.02% sell-side. That is why delivery costs roughly 4x intraday here.
# ---------------------------------------------------------------------------
COSTS = {
    "index_futures":   0.0006,   # ~0.06% round trip: STT 0.02% sell + txn + GST
    "equity_intraday": 0.0010,   # ~0.10% round trip: STT 0.025% sell + the rest
    "equity_delivery": 0.0025,   # ~0.25% round trip: STT 0.1% both sides dominates
}

# Slippage per side, as a fraction of price. Swept, never assumed.
SLIPPAGE_SWEEP = [0.0, 0.0005, 0.0010, 0.0020]

# Which cost bucket each instrument falls in by default.
INSTRUMENT_COST = {
    "NIFTY": "index_futures",
    "NIFTY_REAL": "index_futures",
    "BANKNIFTY": "index_futures",
}
DEFAULT_EQUITY_COST = "equity_delivery"

LABELS = {
    "NIFTY": "NIFTY 50 (derived, 2021-2026)",
    "NIFTY_REAL": "NIFTY 50 (real 5-min, 2024-2026)",
    "BANKNIFTY": "NIFTY Bank (real 5-min, 2024-2026)",
}

# NIFTY lot size is time-varying. Any P&L computed with a single constant
# across 2021-2026 is wrong; strategy_v2.py hardcodes 75 and has exactly that bug.
#
# ⚠ UNVERIFIED — these dates and values are a placeholder, NOT sourced from the
# contract spec. The evidence on hand actually conflicts: strategy_v2.py uses 75,
# the live strangle config uses 65, and the session notes record "75-vs-65" as an
# open question. Do not compute any reported P&L from this table until it has
# been checked against the NSE circulars or a broker contract note per period.
#
# Options P&L scales linearly with lot size, so a wrong entry here does not
# produce a subtle error — it produces a proportionally wrong result.
NIFTY_LOT_SIZE_UNVERIFIED = {
    "2021-01-01": 75,
    "2021-10-29": 50,
    "2024-11-20": 25,
    "2025-12-26": 75,
}
NIFTY_LOT_SIZE = NIFTY_LOT_SIZE_UNVERIFIED


def lot_size_on(date, table: dict = None) -> int:
    """The lot size in force on a given date."""
    table = table or NIFTY_LOT_SIZE
    day = pd.Timestamp(date).normalize()
    applicable = [v for k, v in sorted(table.items()) if pd.Timestamp(k) <= day]
    if not applicable:
        raise ValueError(f"No lot size defined on or before {day.date()}")
    return applicable[-1]


def cost_pct(inst: str, bucket: str = None) -> float:
    """Round-trip structural cost for an instrument, as a fraction of turnover."""
    bucket = bucket or INSTRUMENT_COST.get(inst, DEFAULT_EQUITY_COST)
    return COSTS[bucket]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _base_bars(inst: str) -> pd.DataFrame:
    """Highest-resolution bars available for an instrument, unresampled."""
    if inst == "NIFTY":
        path = INDEX_DIR / "derived_spot_1min.parquet"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing. Build it:\n"
                f"    python -m backtesting.book_strategies.kaufman.build_nifty_spot"
            )
        return pd.read_parquet(path)

    if inst in ("NIFTY_REAL", "BANKNIFTY"):
        root = INDEX_DIR if inst == "NIFTY_REAL" else BANKNIFTY_DIR
        files = sorted(root.glob("[0-9][0-9][0-9][0-9]/ohlcv_5min.parquet"))
        if not files:
            raise FileNotFoundError(f"No 5-min parquets under {root}")
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        return df.set_index("datetime").sort_index()

    # A Nifty-500 constituent, daily.
    path = DAILY_DIR / f"{inst}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"No daily data for {inst!r} at {path}")
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def _session_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Drop anything outside 09:15-15:30. Intraday data only."""
    if not isinstance(df.index, pd.DatetimeIndex) or df.index.time.max() == df.index.time.min():
        return df
    return df.between_time(SESSION_START, SESSION_END)


def get_bars(inst: str, timeframe: str = "1D",
             start: str = None, end: str = None,
             use_cache: bool = True) -> pd.DataFrame:
    """Resampled OHLC bars, optionally date-filtered.

    timeframe accepts any pandas offset: "5min", "15min", "30min", "1h", "1D".
    """
    tag = f"{inst}_{timeframe}"
    cache = CACHE_DIR / f"{tag}.parquet"

    if use_cache and cache.exists():
        df = pd.read_parquet(cache)
    else:
        base = _session_filter(_base_bars(inst))

        daily_source = base.index.normalize().equals(base.index)
        if daily_source or timeframe in ("1D", "D", "1d"):
            if daily_source:
                df = base[["open", "high", "low", "close"]].copy()
            else:
                # Intraday -> daily. No offset: the NSE session sits inside one
                # calendar day, unlike the 24x5 instruments data_multi handles.
                df = base.resample("1D").agg(
                    open=("open", "first"), high=("high", "max"),
                    low=("low", "min"), close=("close", "last"),
                ).dropna(subset=["open", "close"])
        else:
            df = base.resample(timeframe, closed="left", label="left").agg(
                open=("open", "first"), high=("high", "max"),
                low=("low", "min"), close=("close", "last"),
            ).dropna(subset=["open", "close"])
            # Resampling spans the overnight gap and manufactures empty bars
            # between 15:30 and 09:15; the dropna above removes them.

        df = df[["open", "high", "low", "close"]]
        if use_cache:
            df.to_parquet(cache)

    if start:
        df = df[df.index >= start]
    if end:
        df = df[df.index <= end]
    return df


def available_equities() -> list[str]:
    if not DAILY_DIR.exists():
        return []
    return sorted(p.stem for p in DAILY_DIR.glob("*.parquet"))


def coverage(inst: str) -> tuple:
    df = _base_bars(inst)
    return df.index.min(), df.index.max(), len(df)


if __name__ == "__main__":
    print("Instruments\n" + "=" * 70)
    for name in ("NIFTY", "NIFTY_REAL", "BANKNIFTY"):
        try:
            lo, hi, n = coverage(name)
            print(f"  {name:<12} {n:>9,} bars  {str(lo)[:16]} → {str(hi)[:16]}")
        except FileNotFoundError as exc:
            print(f"  {name:<12} unavailable: {exc}")

    eq = available_equities()
    print(f"  {'equities':<12} {len(eq):>9,} symbols (daily)")

    print("\nResampling check — NIFTY\n" + "=" * 70)
    for tf in ("5min", "15min", "1h", "1D"):
        d = get_bars("NIFTY", tf)
        print(f"  {tf:<6} {len(d):>8,} bars  {str(d.index.min())[:16]} → "
              f"{str(d.index.max())[:16]}")

    print("\nCosts (round trip, % of turnover)\n" + "=" * 70)
    for bucket, value in COSTS.items():
        print(f"  {bucket:<18} {value * 100:.3f}%")

    print("\nNIFTY lot size over time\n" + "=" * 70)
    for day in ("2021-06-01", "2022-06-01", "2025-06-01", "2026-06-01"):
        print(f"  {day}  ->  {lot_size_on(day)}")
