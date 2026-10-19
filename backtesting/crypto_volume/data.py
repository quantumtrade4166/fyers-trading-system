"""
Load Binance USDT-M perp bars for the crypto volume backtests.

1h klines (crypto/download_binance_perps.py) resampled to 4h / 1D on UTC
boundaries. Each bar carries:
  open high low close volume quote_volume taker_buy_volume taker_sell_volume
  funding  - sum of funding settlements that fall inside the bar (a long pays
             +funding x notional, a short receives it)
"""

from functools import lru_cache
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[2]
PERPS = ROOT / "data" / "CRYPTO_PERPS"
UNIVERSE = ROOT / "crypto" / "universe" / "universe_top100.csv"

RULE = {"1h": "1h", "4h": "4h", "1d": "1D"}
BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365}


def universe() -> pd.DataFrame:
    return pd.read_csv(UNIVERSE)


@lru_cache(maxsize=None)
def _raw(perp: str) -> tuple[pd.DataFrame, pd.Series]:
    k = pd.read_parquet(PERPS / perp / "klines_1h.parquet").set_index("datetime")
    fp = PERPS / perp / "funding.parquet"
    f = (pd.read_parquet(fp).set_index("datetime")["funding_rate"]
         if fp.exists() else pd.Series(dtype="float64", index=pd.DatetimeIndex([])))
    return k, f


def load(perp: str, tf: str = "4h") -> pd.DataFrame:
    k, f = _raw(perp)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "volume": "sum", "quote_volume": "sum", "taker_buy_volume": "sum"}
    df = k[list(agg)].resample(RULE[tf], label="left", closed="left").agg(agg)
    df = df.dropna(subset=["close"])
    df = df[df["volume"] > 0]
    df["taker_sell_volume"] = df["volume"] - df["taker_buy_volume"]
    # A settlement stamped exactly at a bar boundary belongs to the bar that held
    # the position through it — the one ending there — so shift by an epsilon.
    fund = f.copy()
    fund.index = fund.index - pd.Timedelta(seconds=1)
    df["funding"] = (fund.resample(RULE[tf], label="left", closed="left").sum()
                     .reindex(df.index).fillna(0.0))
    return df


def all_coins() -> list[str]:
    """Every downloaded USDT perp (incl. delisted), minus index/stable/gold contracts."""
    from crypto.download_binance_perps import NOT_COINS
    return sorted(p.name for p in PERPS.iterdir()
                  if p.name.endswith("USDT") and p.name not in NOT_COINS
                  and (p / "klines_1h.parquet").exists())


def available() -> list[str]:
    return [p for p in universe()["perp"] if (PERPS / p / "klines_1h.parquet").exists()]
