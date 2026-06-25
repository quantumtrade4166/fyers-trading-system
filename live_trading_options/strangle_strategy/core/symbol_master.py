"""
core/symbol_master.py
=====================

Resolve exact Fyers option symbols from the public symbol-master CSVs.

We NEVER hand-build option symbols — the monthly expiry (last weekly of the
month) uses a different format (`NIFTY26JUN24250CE`) than the weekly format
(`NIFTY2670724250CE`), and getting it wrong silently fetches nothing. Always
look up the real symbol string from the master.

Masters (cached once per day under data/symbol_master/):
  NSE F&O  -> https://public.fyers.in/sym_details/NSE_FO.csv   (NIFTY etc.)
  BSE F&O  -> https://public.fyers.in/sym_details/BSE_FO.csv   (SENSEX)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt
from pathlib import Path

import pandas as pd

CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "symbol_master"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

MASTER_URL = {
    "NSE": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "BSE": "https://public.fyers.in/sym_details/BSE_FO.csv",
}
# which exchange master holds each index's options
INDEX_EXCHANGE = {"NIFTY": "NSE", "SENSEX": "BSE"}

# raw CSV column positions we rely on (file has no header)
_COL = {"desc": 1, "lot": 3, "symbol": 9, "underlying": 13, "strike": 15, "opt_type": 16}


def _load_master(exchange: str) -> pd.DataFrame:
    cache = CACHE_DIR / f"{exchange}_{dt.date.today().isoformat()}.csv"
    if cache.exists():
        raw = pd.read_csv(cache, header=None, dtype=str)
    else:
        raw = pd.read_csv(MASTER_URL[exchange], header=None, dtype=str)
        raw.to_csv(cache, header=False, index=False)
    df = pd.DataFrame({
        "desc":       raw[_COL["desc"]],
        "lot":        pd.to_numeric(raw[_COL["lot"]], errors="coerce"),
        "symbol":     raw[_COL["symbol"]],
        "underlying": raw[_COL["underlying"]],
        "strike":     pd.to_numeric(raw[_COL["strike"]], errors="coerce"),
        "opt_type":   raw[_COL["opt_type"]],
    })
    df = df[df["opt_type"].isin(["CE", "PE"])].copy()
    # expiry parsed from description "NIFTY 30 Jun 26 24250 CE" -> 2026-06-30
    parts = df["desc"].str.split()
    df["expiry"] = pd.to_datetime(
        parts.str[1] + " " + parts.str[2] + " " + parts.str[3],
        format="%d %b %y", errors="coerce"
    ).dt.date
    return df.dropna(subset=["expiry", "strike"])


def options(index: str) -> pd.DataFrame:
    """All live option rows for one index (exact-underlying match)."""
    exch = INDEX_EXCHANGE[index]
    df = _load_master(exch)
    return df[df["underlying"] == index].copy()


def lot_size(index: str) -> int:
    return int(options(index)["lot"].mode().iloc[0])


def nearest_expiry(index: str, on_or_after: dt.date = None) -> dt.date:
    on_or_after = on_or_after or dt.date.today()
    exps = sorted(e for e in options(index)["expiry"].unique() if e >= on_or_after)
    if not exps:
        raise RuntimeError(f"No expiry on/after {on_or_after} for {index}")
    return exps[0]


def find_symbol(index: str, expiry: dt.date, strike: int, opt_type: str) -> str | None:
    df = options(index)
    hit = df[(df["expiry"] == expiry) & (df["strike"] == float(strike)) &
             (df["opt_type"] == opt_type)]
    return None if hit.empty else hit["symbol"].iloc[0]


def available_strikes(index: str, expiry: dt.date, opt_type: str = "CE") -> list[int]:
    df = options(index)
    hit = df[(df["expiry"] == expiry) & (df["opt_type"] == opt_type)]
    return sorted(int(s) for s in hit["strike"].unique())
