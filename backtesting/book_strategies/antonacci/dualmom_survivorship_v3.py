"""
dualmom_survivorship_v3.py — the honest DualMom run (v2 run 4) redone on
REPAIRED bhavcopy prices.   (2026-11-02)

    .venv\\Scripts\\python.exe -m backtesting.book_strategies.antonacci.dualmom_survivorship_v3

The NSE corporate-actions register behind close_adj.parquet misses some
splits/bonuses and has no special dividends or demergers (e.g. JSW Steel 1:10
Jan-2017, Majesco Rs974 dividend Dec-2020). Each looks like a -50%..-99%
one-day crash — and with DualMom's -35% stop it is SOLD at the fake price.

Same rule as engine/bhav_fix.py, on closes: a close-to-close move below -45%
or above +80%, with <= 30 calendar days since the stock's previous close, is
a corporate action -> all earlier closes are scaled.

  A  v2 run 4 (point-in-time top 500 incl. dead) — as published 24-Oct
  B  A + repaired prices
  C  B + ETFs removed from the universe (live DualMom trades stocks only)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

from backtesting.book_strategies.antonacci import dualmom_final as F
from backtesting.book_strategies.antonacci.dualmom_survivorship_v2 import (
    backtest, stats, MATRIX, UNIVERSE_N, TURNOVER_WIN, OUT)

DOWN, UP, MAX_GAP = -0.45, 0.80, 30


def repair(close: pd.DataFrame):
    out = close.copy()
    ev = []
    for sym in close.columns:
        s = close[sym].dropna()
        s = s[s > 0]
        if len(s) < 2:
            continue
        r = s / s.shift(1) - 1
        gap = s.index.to_series().diff().dt.days
        hit = r[((r < DOWN) | (r > UP)) & (gap <= MAX_GAP)]
        for d, x in hit.items():
            f = 1 + x
            out.loc[out.index < d, sym] *= f
            ev.append({"symbol": sym, "date": d.date(), "move_pct": round(100 * x, 1)})
    return out, pd.DataFrame(ev)


def etf_names(cols):
    import re
    from backtesting.book_strategies.schwager.stock_universe import _ETF_DIR, _PAT
    etfs = {p.stem.upper() for p in _ETF_DIR.glob("*.parquet")}
    return {c for c in cols if c.upper() in etfs or _PAT.search(c.upper())}


if __name__ == "__main__":
    bh = pd.read_parquet(MATRIX / "close_adj.parquet").loc[F.START_DATE:].astype("float64")
    val = pd.read_parquet(MATRIX / "value.parquet").loc[F.START_DATE:].astype("float64")
    turnover = val.rolling(TURNOVER_WIN, min_periods=60).mean()
    fixed, ev = repair(bh)
    ev.to_csv(OUT / "dualmom_v3_repaired_events.csv", index=False)
    etfs = etf_names(bh.columns)
    print(f"repaired {len(ev)} jumps in {ev.symbol.nunique()} symbols; {len(etfs)} ETF symbols", flush=True)

    def pit(px, drop):
        def fn(i, lb):
            tv = turnover.iloc[i].dropna()
            elig = px.iloc[i].dropna().index.intersection(px.iloc[lb].dropna().index)
            tv = tv.reindex(elig).dropna()
            if drop:
                tv = tv[~tv.index.isin(drop)]
            return tv.nlargest(UNIVERSE_N).index
        return fn

    runs = [("A honest v2 (as published)", bh, pit(bh, None)),
            ("B + repaired prices", fixed, pit(fixed, None)),
            ("C + repaired + no ETFs", fixed, pit(fixed, etfs))]
    rows, navs = [], {}
    for name, px, fn in runs:
        nav, log, stops, held = backtest(px, fn)
        navs[name] = nav
        rows.append({"run": name, **{f"ALL_{k}": v for k, v in stats(nav).items()},
                     **{f"IS_{k}": v for k, v in stats(nav, b="2019-01-01").items()},
                     **{f"OOS_{k}": v for k, v in stats(nav, a="2019-01-01").items()},
                     "stops": len(stops), "etf_positions": sum(s in etfs for s in held)})
        print(f"done: {name}", flush=True)
    s = pd.DataFrame(rows)
    s.to_csv(OUT / "dualmom_survivorship_v3.csv", index=False)
    pd.DataFrame(navs).to_csv(OUT / "dualmom_survivorship_v3_nav.csv")
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(s.to_string(index=False))
