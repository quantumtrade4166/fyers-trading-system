"""
Orderflow bars from Binance 1-min klines.

buy   = taker_buy_volume (aggressive buyers)      sell = volume - buy
delta = buy - sell        dr = delta / volume (-1..+1)       cvd = cumsum(delta)
"""
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parents[2]
DATA = ROOT / "data" / "CRYPTO_PERPS"


@lru_cache(maxsize=8)
def load_1m(perp: str) -> pd.DataFrame:
    df = pd.read_parquet(DATA / perp / "klines_1m.parquet")
    return df.set_index("datetime").sort_index()


def make_bars(perp: str, tf: str) -> pd.DataFrame:
    m = load_1m(perp)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "volume": "sum", "taker_buy_volume": "sum", "trades": "sum"}
    b = m.resample(tf, label="left", closed="left").agg(agg).dropna(subset=["open"])
    b = b[b["volume"] > 0].copy()
    b["buy"] = b["taker_buy_volume"]
    b["sell"] = b["volume"] - b["buy"]
    b["delta"] = b["buy"] - b["sell"]
    b["dr"] = b["delta"] / b["volume"]
    b["cvd"] = b["delta"].cumsum()
    v = b["volume"]
    b["vol_z"] = (v - v.rolling(96).mean()) / v.rolling(96).std()
    pc = b["close"].shift()
    tr = np.maximum(b["high"] - b["low"],
                    np.maximum((b["high"] - pc).abs(), (b["low"] - pc).abs()))
    b["atr"] = tr.rolling(14).mean()
    b["ema20"] = b["close"].ewm(span=20, adjust=False).mean()
    return b.dropna()
