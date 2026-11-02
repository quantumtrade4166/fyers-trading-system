import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC grid bot sweep.
Run: .venv\\Scripts\\python.exe -m backtesting.crypto_grid.run_grid
Out: backtesting/crypto_grid/results/
"""

import itertools
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.crypto_grid.grid_engine import (GridConfig, build_path, daily_funding,
                                                 load_btc_1m, metrics, run_grid)

OUT = Path(__file__).parent / "results"
OUT.mkdir(exist_ok=True)
START, END = "2018-01-01", "2026-11-01"
CAP = 10_000.0

DELTA_MAKER = 0.0002 * 1.18     # 0.02% + 18% GST
DELTA_TAKER = 0.0005 * 1.18
VENUES = {
    "spot_long_binance":  dict(style="long",    maker_fee=0.001,       taker_fee=0.001,       funding=False),
    "perp_long_delta":    dict(style="long",    maker_fee=DELTA_MAKER, taker_fee=DELTA_TAKER, funding=True),
    "perp_neutral_delta": dict(style="neutral", maker_fee=DELTA_MAKER, taker_fee=DELTA_TAKER, funding=True),
}
STEPS = [0.005, 0.01, 0.02, 0.03]
RANGES = [0.10, 0.20, 0.30, 0.50]


def yearly(eq: pd.Series) -> pd.Series:
    ye = eq.resample("YE").last()
    prev = ye.shift(1); prev.iloc[0] = CAP
    return (ye / prev - 1) * 100


def main():
    t0 = time.time()
    df = load_btc_1m(START, END)
    path, day4, last = build_path(df)
    days = pd.DatetimeIndex(np.unique(day4))
    fund = daily_funding(days)
    print(f"{len(df):,} bars {df.datetime.iloc[0]} -> {df.datetime.iloc[-1]}  ({time.time()-t0:.0f}s)")

    # buy & hold benchmark
    close = df.set_index("datetime")["close"].resample("D").last().dropna()
    bh = close / close.iloc[0] * CAP
    rows = [{"venue": "buy_hold", "step_%": None, "range_%": None, **metrics(bh, CAP)}]
    yr = {"buy_hold": yearly(bh)}
    curves = {"buy_hold": bh}

    # 1) trailing grid (re-centres when price leaves the window), full history
    for (vname, v), step, rng in itertools.product(VENUES.items(), STEPS, RANGES):
        cfg = GridConfig(step=step, half_range=rng, restart="exit", capital=CAP, **v)
        daily, st = run_grid(path, day4, last, cfg, fund)
        m = metrics(daily["equity"], CAP)
        key = f"{vname}|{step*100:g}|{rng*100:g}"
        rows.append({"venue": vname, "step_%": step * 100, "range_%": rng * 100, **m,
                     "restarts": st["restarts"], "round_trips": st["round_trips"],
                     "fees": st["fees"]})
        yr[key] = yearly(daily["equity"])
        curves[key] = daily["equity"]
        print(f"  {key:32s} final {m['final']:>12,.0f}  CAGR {m['cagr_%']:6.1f}%  "
              f"DD {m['max_dd_%']:6.1f}%  restarts {st['restarts']:4d}  trips {st['round_trips']:7,d}"
              f"  ({time.time()-t0:.0f}s)")
    res = pd.DataFrame(rows)
    res.to_csv(OUT / "sweep_trailing.csv", index=False)
    pd.DataFrame(yr).T.round(1).to_csv(OUT / "sweep_trailing_yearly.csv")
    pd.DataFrame(curves).to_csv(OUT / "sweep_trailing_equity.csv")

    # 2) fixed grid started fresh each 1-Jan (no restart), one year at a time
    yrows = []
    for y in range(2018, 2027):
        m = (df.datetime >= f"{y}-01-01") & (df.datetime < f"{y+1}-01-01")
        sub = df[m]
        if sub.empty:
            continue
        p, d, l = build_path(sub)
        for (vname, v), step, rng in itertools.product(VENUES.items(), STEPS, RANGES):
            cfg = GridConfig(step=step, half_range=rng, restart="none", capital=CAP, **v)
            daily, st = run_grid(p, d, l, cfg, fund)
            yrows.append({"year": y, "venue": vname, "step_%": step * 100, "range_%": rng * 100,
                          "return_%": (daily["equity"].iloc[-1] / CAP - 1) * 100,
                          "max_dd_%": (daily["equity"] / daily["equity"].cummax() - 1).min() * 100,
                          "round_trips": st["round_trips"]})
        bh_y = sub["close"].iloc[-1] / sub["open"].iloc[0] * 100 - 100
        yrows.append({"year": y, "venue": "buy_hold", "return_%": bh_y})
        print(f"  fixed grids {y} done ({time.time()-t0:.0f}s)")
    pd.DataFrame(yrows).to_csv(OUT / "fixed_by_year.csv", index=False)
    print(f"done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
