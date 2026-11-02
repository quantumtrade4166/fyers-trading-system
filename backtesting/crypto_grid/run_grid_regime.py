import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Grid bot that only runs in sideways markets.
A daily "sideways" filter is computed from data up to YESTERDAY's close (no
look-ahead). Filter ON -> grid runs; OFF -> close everything, sit in cash.
In-sample 2018-2022, out-of-sample 2023 -> Oct-2026.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_grid.run_grid_regime
"""

import itertools
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.crypto_grid.grid_engine import (GridConfig, build_path, daily_funding,
                                                 load_btc_1m, run_grid)
from backtesting.crypto_grid.run_grid import CAP, DELTA_MAKER, DELTA_TAKER

OUT = Path(__file__).parent / "results"
START, END = "2018-01-01", "2026-11-01"
SPLIT = "2023-01-01"

VENUES = {
    "spot_long_binance":  dict(style="long",    maker_fee=0.001,       taker_fee=0.001,       funding=False),
    "perp_neutral_delta": dict(style="neutral", maker_fee=DELTA_MAKER, taker_fee=DELTA_TAKER, funding=True),
}
STEPS = [0.01, 0.02, 0.03]
RANGES = [0.10, 0.20]
RESTARTS = ["exit", "pause"]


# ------------------------------------------------------------------ daily filters
def daily_bars(df):
    d = df.set_index("datetime").resample("D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    return d


def efficiency_ratio(c, n):
    return (c - c.shift(n)).abs() / c.diff().abs().rolling(n).sum()


def adx(d, n=14):
    h, l, c = d["high"], d["low"], d["close"]
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(pdm, d.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    ndi = 100 * pd.Series(ndm, d.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def chop(d, n=14):
    h, l, c = d["high"], d["low"], d["close"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return 100 * np.log10(tr.rolling(n).sum() / (h.rolling(n).max() - l.rolling(n).min())) / np.log10(n)


def build_filters(d):
    c = d["close"]
    rng14 = (d["high"].rolling(14).max() - d["low"].rolling(14).min()) / c
    f = {
        "none":        pd.Series(True, d.index),
        "ER10<0.2":    efficiency_ratio(c, 10) < 0.2,
        "ER20<0.2":    efficiency_ratio(c, 20) < 0.2,
        "ER20<0.3":    efficiency_ratio(c, 20) < 0.3,
        "ADX14<20":    adx(d) < 20,
        "ADX14<25":    adx(d) < 25,
        "Range14<10%": rng14 < 0.10,
        "Range14<15%": rng14 < 0.15,
        "CHOP14>55":   chop(d) > 55,
        "CHOP14>61.8": chop(d) > 61.8,
    }
    # decide today from yesterday's close
    return {k: v.shift(1).fillna(False).astype(bool) for k, v in f.items()}


def period_stats(eq, start, end):
    e = eq[(eq.index >= start) & (eq.index < end)]
    prev = eq[eq.index < start]
    e0 = prev.iloc[-1] if len(prev) else CAP
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    tot = e.iloc[-1] / e0
    cagr = (tot ** (1 / yrs) - 1) * 100 if tot > 0 else -100.0
    full = pd.concat([pd.Series([e0]), e.reset_index(drop=True)])
    dd = (full / full.cummax() - 1).min() * 100
    return cagr, dd


def main():
    t0 = time.time()
    df = load_btc_1m(START, END)
    path, day4, last = build_path(df)
    days = pd.DatetimeIndex(np.unique(day4))
    fund = daily_funding(days)
    d = daily_bars(df)
    filters = build_filters(d)
    day_idx = pd.DatetimeIndex(day4)
    print(f"data ready ({time.time()-t0:.0f}s)")
    for k, v in filters.items():
        print(f"  {k:12s} ON {v.mean()*100:5.1f}% of days")

    bh = d["close"] / d["close"].iloc[0]
    half = (1 + 0.5 * bh.pct_change().fillna(0)).cumprod() * CAP    # 50/50 rebalanced
    rows = []
    for nm, eq in [("50/50 BTC-cash", half), ("buy_hold", bh * CAP)]:
        ci, di = period_stats(eq, START, SPLIT); co, do = period_stats(eq, SPLIT, "2026-11-02")
        rows.append({"venue": nm, "filter": "-", "IS_cagr": ci, "IS_dd": di, "OOS_cagr": co, "OOS_dd": do})

    for fname, sig in filters.items():
        active = sig.reindex(day_idx).fillna(False).to_numpy()
        for (vname, v), step, rng, rs in itertools.product(VENUES.items(), STEPS, RANGES, RESTARTS):
            if fname == "none" and rs == "pause":
                continue
            cfg = GridConfig(step=step, half_range=rng, restart=rs, capital=CAP, **v)
            daily, st = run_grid(path, day4, last, cfg, fund, active=active)
            eq = daily["equity"]
            ci, di = period_stats(eq, START, SPLIT)
            co, do = period_stats(eq, SPLIT, "2026-11-02")
            rows.append({"venue": vname, "filter": fname, "step_%": step * 100, "range_%": rng * 100,
                         "restart": rs, "IS_cagr": ci, "IS_dd": di, "OOS_cagr": co, "OOS_dd": do,
                         "days_on_%": st["days_on"] / len(days) * 100, "round_trips": st["round_trips"]})
        print(f"  {fname:12s} done ({time.time()-t0:.0f}s)")
    res = pd.DataFrame(rows).round(2)
    res.to_csv(OUT / "regime_sweep.csv", index=False)
    print(f"done in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
