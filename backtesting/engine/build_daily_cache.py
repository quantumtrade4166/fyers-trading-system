import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Build daily OHLC bars (22:00 UTC session, like data_multi) for instruments
whose Kaufman cache is missing or stale, straight from the Google Drive
1-minute / 1-second archives. Output: backtesting/engine/_cache_daily/{inst}.parquet

    .venv\Scripts\python.exe -m backtesting.engine.build_daily_cache audusd usdchf usdcad xauusd
"""

import importlib
from pathlib import Path

import pandas as pd

DRIVE = Path("D:/My Drive")
OUT = Path(__file__).resolve().parent / "_cache_daily"
OUT.mkdir(exist_ok=True)
AGG = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"))


def _daily(m1: pd.DataFrame) -> pd.DataFrame:
    d = m1.resample("1D", closed="left", label="left", offset="22h").agg(**AGG).dropna(subset=["open", "close"])
    n = m1["close"].resample("1D", closed="left", label="left", offset="22h").count().reindex(d.index)
    d = d[n >= 60]                       # drop stub sessions (holidays, Sunday opens)
    d.index = (d.index + pd.Timedelta(hours=2)).normalize()   # label = trading date
    return d[~d.index.duplicated(keep="last")]


def dukascopy(inst: str) -> pd.DataFrame:
    files = sorted((DRIVE / "Dukascopy_1m" / inst).rglob("ohlcv_1m_*.parquet"))
    parts = []
    for f in files:
        x = pd.read_parquet(f)
        if "datetime" in x.columns:
            x = x.set_index("datetime")
        parts.append(x[["open", "high", "low", "close"]])
    m1 = pd.concat(parts).sort_index()
    m1 = m1[~m1.index.duplicated(keep="first")]
    return _daily(m1)


def xauusd() -> pd.DataFrame:
    L = importlib.import_module("forex.xauusd_data_loader")
    L.RAW_DIR = DRIVE / "XAUUSD_1s_Data"
    m1 = L.load_xauusd("2003-05-01", "2026-12-31", timeframe="1min")
    return _daily(m1)


if __name__ == "__main__":
    for inst in sys.argv[1:]:
        d = xauusd() if inst == "xauusd" else dukascopy(inst)
        d.to_parquet(OUT / f"{inst}.parquet")
        print(f"{inst}: {len(d)} days {d.index[0].date()} -> {d.index[-1].date()}", flush=True)
