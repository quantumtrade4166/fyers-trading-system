import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Survivorship-free NSE daily data from the bhavcopy archive.

    .venv\Scripts\python.exe -m backtesting.engine.bhav        (build once)

Source : Bhavcopy/{year}/{date}.parquet — every NSE stock that traded that
         day (incl. companies that later crashed, merged or delisted)
Adjust : NSE corporate-actions register (Bhavcopy/_matrix/corporate_actions.csv,
         splits + bonuses): prices before an ex-date x factor, volume / factor
         (turnover unchanged) — same method as antonacci/build_bhavcopy_matrix.py
Universe "tradable": point-in-time stand-in for the Nifty 500 — on each day,
         the 500 stocks with the highest average traded value over the
         previous 63 sessions, using ONLY data up to the day before.
Output : Bhavcopy/_ohlcv/{SYMBOL}.parquet  (open high low close volume value
         adv20 tradable), one per symbol with >= 250 sessions.

Known limits: a renamed symbol starts a new history (its old name looks
delisted); corporate actions missing from the register leave a fake gap.
"""

import glob
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
BHAV = ROOT / "Bhavcopy"
CA = BHAV / "_matrix" / "corporate_actions.csv"
OUT = BHAV / "_ohlcv"
TOP_N, LOOKBACK, MIN_ROWS = 500, 63, 250


def build():
    t0 = time.time()
    files = sorted(f for f in glob.glob(str(BHAV / "*" / "*.parquet")) if Path(f).parent.name.isdigit())
    parts = []
    for f in files:
        d = pd.read_parquet(f, columns=["symbol", "series", "open", "high", "low", "close", "volume", "value"])
        d = d[d["series"].isin(["EQ", "BE", "BZ"])]
        d["date"] = pd.Timestamp(Path(f).stem)
        parts.append(d.drop(columns="series"))
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="first")
    print(f"read {len(files)} days, {len(df):,} rows, {df.symbol.nunique():,} symbols ({time.time()-t0:.0f}s)")

    # point-in-time liquidity rank -> tradable
    val = df.pivot(index="date", columns="symbol", values="value").astype("float64")
    avg = val.rolling(LOOKBACK, min_periods=20).mean().shift(1)     # known before the day opens
    rank = avg.rank(axis=1, ascending=False, method="first")
    tradable = (rank <= TOP_N)
    print(f"universe: median {int(tradable.sum(axis=1).median())} names/day, "
          f"{int(tradable.any().sum()):,} different stocks ever in the top {TOP_N}")

    ca = pd.read_csv(CA, parse_dates=["ex_date"])
    OUT.mkdir(exist_ok=True)
    n_out, fake = 0, 0
    for sym, g in df.groupby("symbol", sort=False):
        if len(g) < MIN_ROWS:
            continue
        g = g.set_index("date")[["open", "high", "low", "close", "volume", "value"]].astype(float)
        fac = pd.Series(1.0, index=g.index)
        for ex, f in ca.loc[ca.symbol == sym, ["ex_date", "factor"]].itertuples(index=False):
            fac[fac.index < ex] *= f
        for c in ("open", "high", "low", "close"):
            g[c] = g[c] * fac
        g["volume"] = g["volume"] / fac
        g["adv20"] = g["value"].rolling(20, min_periods=5).mean().shift(1)
        g["tradable"] = tradable[sym].reindex(g.index).fillna(False).astype(bool)
        r = g["open"] / g["close"].shift(1)
        fake += int(((r < 0.6) | (r > 1.67)).sum())
        g = g[(g["low"] > 0) & (g["high"] >= g["low"])]
        g.to_parquet(OUT / f"{sym}.parquet")
        n_out += 1
    print(f"wrote {n_out:,} symbols to {OUT}  | overnight jumps beyond -40%/+67% left after adjustment: {fake:,}"
          f"  ({(time.time()-t0)/60:.1f} min)")


def list_symbols() -> list[str]:
    return sorted(p.stem for p in OUT.glob("*.parquet"))


def load(sym: str) -> pd.DataFrame:
    return pd.read_parquet(OUT / f"{sym}.parquet")


if __name__ == "__main__":
    build()
