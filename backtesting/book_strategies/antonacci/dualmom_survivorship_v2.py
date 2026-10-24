"""
dualmom_survivorship_v2.py — the LIVE DualMom rules on survivorship-free data,
with the bias split into its parts.   (2026-10-24)

    .venv\\Scripts\\python.exe -m backtesting.book_strategies.antonacci.dualmom_survivorship_v2

Why v2: survivorship_test.py (2026-09-01) held the universe RULE constant and
only removed dead stocks (-2.95 pts). It never compared against the list the
headline backtest actually uses — TODAY's Nifty 500 — which also contains
"look-ahead membership": stocks that are in the index today BECAUSE they rose.
It also ran without the live -35% stop and 10% weight cap.

Every run below uses the EXACT canonical engine of dualmom_final.py
(top 40 by 12m return, momentum weights capped at 10%, whole shares, 2x
ceiling, -35% intra-month stop, Nifty 50 > 100d MA filter, 6% liquid fund,
0.1% slippage). Only the stock list / price source changes:

  1  today's Nifty 500,  Fyers prices          (= the canonical headline)
  2  today's Nifty 500,  bhavcopy prices       (1→2: data source only)
  3  point-in-time top 500 by turnover, survivors only, bhavcopy
  4  point-in-time top 500 by turnover, INCLUDING dead stocks, bhavcopy
                                               (= the honest number)
  2→4 = total universe bias ; 3→4 = dead-stock part ; 2→3 = membership part
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.book_strategies.antonacci import dualmom_final as F

MATRIX = F.ROOT / "Bhavcopy" / "_matrix"
UNIVERSE_N, TURNOVER_WIN = 500, 250
OUT = Path(__file__).parent / "results"


def backtest(close, universe_fn, top_n=40, capital=1_000_000, start=None, end=None):
    """dualmom_final.backtest, verbatim logic, with r12 restricted to universe_fn(i)."""
    nifty, nifty_ma = F.load_nifty()
    ffill = close.ffill(limit=F.MAX_STALE_DAYS)
    month_ends = close.resample("ME").last().index
    if end is not None:
        month_ends = month_ends[month_ends <= pd.Timestamp(end)]
    cash_base, base_date = float(capital), month_ends[0]
    held, entry, prev = {}, {}, month_ends[0]
    daily, log, stops, held_syms = {}, [], [], []
    for rd in month_ends[1:]:
        i = close.index.get_indexer([rd], method="ffill")[0]
        if i < 0:
            continue
        rd = close.index[i]
        seg = ffill.loc[prev:rd]
        for d in seg.index:
            row = seg.loc[d]
            if held:
                for sym in list(held):
                    p = row.get(sym, np.nan)
                    if pd.isna(p) or p > entry[sym] * (1 - F.STOP_LOSS):
                        continue
                    cash_base = F._grow(cash_base, base_date, d); base_date = d
                    cash_base += held[sym] * p * (1 - F.SLIPPAGE_PCT)
                    stops.append(sym)
                    del held[sym], entry[sym]
            daily[d] = F._grow(cash_base, base_date, d) + sum(
                held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))
        cash = F._grow(cash_base, base_date, rd)
        mark = ffill.iloc[i]
        lb = i - F.LOOKBACK
        if lb < 0:
            cash_base, base_date, prev = cash, rd, rd
            continue
        ni = nifty.index.get_indexer([rd], method="ffill")[0]
        up = (not pd.isna(nifty_ma.iloc[ni])) and nifty.iloc[ni] > nifty_ma.iloc[ni]
        r12 = (close.iloc[i] / close.iloc[lb] - 1).dropna()
        uni = universe_fn(i, lb)
        if uni is not None:
            r12 = r12[r12.index.isin(uni)]
        proceeds = cash
        for sym, sh in held.items():
            p = mark.get(sym, np.nan)
            if not pd.isna(p):
                proceeds += sh * p * (1 - F.SLIPPAGE_PCT)
        held, entry = {}, {}
        if up:
            top = r12.nlargest(top_n)
            w = F.cap_weights({s: max(v, 0.001) for s, v in top.items()}, F.MAX_WEIGHT)
            eff = mark * (1 + F.SLIPPAGE_PCT)
            held = F.allocate(w, eff, proceeds)
            for s in held:
                entry[s] = float(eff[s])
            held_syms.extend(held)
            cash_base = max(proceeds - sum(n * eff[s] for s, n in held.items()), 0)
            log.append({"date": rd.date(), "signal": "IN", "names": len(held)})
        else:
            cash_base = proceeds
            log.append({"date": rd.date(), "signal": "OUT", "names": 0})
        base_date, prev = rd, rd
    for d in ffill.loc[prev:].index:
        row = ffill.loc[d]
        daily[d] = F._grow(cash_base, base_date, d) + sum(
            held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))
    nav = pd.Series(daily).sort_index()
    nav = nav[nav > 0]
    return nav, pd.DataFrame(log), stops, held_syms


def stats(nav, a=None, b=None):
    x = nav
    if a: x = x[x.index >= pd.Timestamp(a)]
    if b: x = x[x.index < pd.Timestamp(b)]
    yrs = (x.index[-1] - x.index[0]).days / 365.25
    rm = x.resample("ME").last().pct_change().dropna()
    return {"cagr_%": round(((x.iloc[-1] / x.iloc[0]) ** (1 / yrs) - 1) * 100, 2),
            "sharpe": round(rm.mean() / rm.std() * np.sqrt(12), 3),
            "maxdd_%": round((x / x.cummax() - 1).min() * 100, 1),
            "end_cr": round(x.iloc[-1] / x.iloc[0] * 10 / 100, 2)}   # Rs10L start, in crore


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    fy = F.load("close")
    bh = pd.read_parquet(MATRIX / "close_adj.parquet").loc[F.START_DATE:].astype("float64")
    val = pd.read_parquet(MATRIX / "value.parquet").loc[F.START_DATE:].astype("float64")
    turnover = val.rolling(TURNOVER_WIN, min_periods=60).mean()
    alive = set(bh.iloc[-1].dropna().index)
    today = set(fy.columns)
    print(f"Fyers today-list {len(today)} | bhavcopy {bh.shape[1]} symbols, {len(alive)} alive, "
          f"{bh.shape[1]-len(alive)} dead | today-list found in bhavcopy: {len(today & set(bh.columns))}")

    def pit(survivors_only):
        def fn(i, lb):
            tv = turnover.iloc[i].dropna()
            elig = bh.iloc[i].dropna().index.intersection(bh.iloc[lb].dropna().index)
            tv = tv.reindex(elig).dropna()
            if survivors_only:
                tv = tv[tv.index.isin(alive)]
            return tv.nlargest(UNIVERSE_N).index
        return fn

    runs = [("1 today's Nifty 500 · Fyers (headline)", fy, lambda i, lb: None),
            ("2 today's Nifty 500 · bhavcopy", bh, lambda i, lb: today),
            ("3 point-in-time top 500 · survivors only", bh, pit(True)),
            ("4 point-in-time top 500 · incl. DEAD (honest)", bh, pit(False))]
    rows, navs = [], {}
    for name, px, fn in runs:
        nav, log, stops, held = backtest(px, fn)
        navs[name] = nav
        dead_share = (np.mean([s not in alive for s in held]) * 100) if px is bh else 0.0
        r = {"run": name, **{f"ALL_{k}": v for k, v in stats(nav).items()},
             **{f"IS_{k}": v for k, v in stats(nav, b="2019-01-01").items()},
             **{f"OOS_{k}": v for k, v in stats(nav, a="2019-01-01").items()},
             "stops": len(stops), "pct_positions_in_dead": round(dead_share, 1)}
        rows.append(r)
        print(f"done: {name}", flush=True)
    s = pd.DataFrame(rows)
    s.to_csv(OUT / "dualmom_survivorship_v2.csv", index=False)
    pd.DataFrame(navs).to_csv(OUT / "dualmom_survivorship_v2_nav.csv")
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(s.to_string(index=False))
    c = s.set_index("run")["ALL_cagr_%"].to_list()
    print(f"\nData source (1→2): {c[1]-c[0]:+.2f} pts | membership/look-ahead (2→3): {c[2]-c[1]:+.2f} | "
          f"dead stocks (3→4): {c[3]-c[2]:+.2f} | TOTAL universe bias (2→4): {c[3]-c[1]:+.2f}")
