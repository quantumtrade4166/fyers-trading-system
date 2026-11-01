import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #2 (Schwager, A Complete Guide to the Futures Market) — daily markets.

Schwager tests on a diversified futures portfolio, so we build the closest
thing on disk: Indian index, crypto, currencies, metals, energy, US index.

Market      Source                                         From      Cost/side
NIFTY       nifty_daily.parquet (index, traded as fut.)   2007-09   0.02%
BANKNIFTY   BANKBEES ETF daily (index proxy)              2007-03   0.02%
BTC         Binance 1-min -> UTC daily                    2017-08   0.06% + 10%/yr funding on longs
EURUSD..    Dukascopy 1-min -> 22:00-UTC daily            2003-05   0.006%
XAUUSD      Dukascopy 1-sec archive -> daily              2003-05   0.015%
XAGUSD      Dukascopy                                     2003-05   0.035%
WTI/BRENT   Dukascopy CFD                                 2010/11   0.025%
SP500/NDX   Dukascopy CFD                                 2011-09   0.008%
"""

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
KAUF = ROOT / "backtesting" / "book_strategies" / "kaufman"
DAILY = ROOT / "backtesting" / "engine" / "_cache_daily"

# name: (loader key, cost per side, funding APR on longs, group)
MARKETS = {
    "NIFTY":     ("nifty", 0.0002, 0.0, "India"),
    "BANKNIFTY": ("etf:BANKBEES", 0.0002, 0.0, "India"),
    "BTC":       ("btc", 0.0006, 0.10, "Crypto"),
    "EURUSD":    ("multi:eurusd", 0.00006, 0.0, "FX"),
    "GBPUSD":    ("multi:gbpusd", 0.00006, 0.0, "FX"),
    "USDJPY":    ("multi:usdjpy", 0.00006, 0.0, "FX"),
    "AUDUSD":    ("daily:audusd", 0.00006, 0.0, "FX"),
    "USDCHF":    ("daily:usdchf", 0.00006, 0.0, "FX"),
    "USDCAD":    ("daily:usdcad", 0.00006, 0.0, "FX"),
    "GOLD":      ("daily:xauusd", 0.00015, 0.0, "Metals"),
    "SILVER":    ("multi:xagusd", 0.00035, 0.0, "Metals"),
    "WTI":       ("multi:lightcmdusd", 0.00025, 0.0, "Energy"),
    "BRENT":     ("multi:brentcmdusd", 0.00025, 0.0, "Energy"),
    "SP500":     ("multi:usa500idxusd", 0.00008, 0.0, "US index"),
    "NASDAQ100": ("multi:usatechidxusd", 0.00008, 0.0, "US index"),
}
IS_END = "2016-12-31"          # choose on data up to here, judge on 2017 ->

_cache = {}


def _clean(d: pd.DataFrame) -> pd.DataFrame:
    d = d[["open", "high", "low", "close"]].astype(float).dropna()
    d.index = pd.to_datetime(d.index).tz_localize(None).normalize()
    d = d[~d.index.duplicated(keep="last")].sort_index()
    # repair impossible bars (open/close outside high/low)
    d["high"] = d[["open", "high", "low", "close"]].max(axis=1)
    d["low"] = d[["open", "high", "low", "close"]].min(axis=1)
    return d


def load(name: str) -> pd.DataFrame:
    if name in _cache:
        return _cache[name]
    key = MARKETS[name][0]
    if key == "nifty":
        d = pd.read_parquet(ROOT / "backtesting/equity_trend_following/55day_breakout/nifty_daily.parquet")
    elif key.startswith("etf:"):
        d = pd.read_parquet(ROOT / "ETF data" / f"{key[4:]}.parquet")
    elif key == "btc":
        d = pd.read_parquet(KAUF / "_cache_btc/btcusdt_1D.parquet")
    elif key.startswith("multi:"):
        # UTC-midnight days with ~2h Sunday stub bars; stubs dropped below
        d = pd.read_parquet(KAUF / "_cache_multi" / f"{key[6:]}_1D.parquet")
    else:
        d = pd.read_parquet(DAILY / f"{key[6:]}.parquet")
    d = _clean(d)
    if name != "BTC":
        d = d[d.index.dayofweek < 5]
    _cache[name] = d
    return d


def available() -> list:
    out = []
    for m in MARKETS:
        try:
            if len(load(m)) > 500:
                out.append(m)
        except Exception as e:                               # missing cache
            print(f"  [skip] {m}: {e}")
    return out
