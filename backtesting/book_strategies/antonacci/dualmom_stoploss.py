"""
dualmom_stoploss.py
DualMom variant: cut a holding intra-month if it falls X% below its entry price.

Rationale: a stock down sharply inside a single month may be telling you
something is structurally wrong. The base strategy only reacts at the next
month-end, so it rides the whole fall.

Rules
  - checked daily on the close
  - on breach: sell at that day's close, less slippage
  - proceeds sit in CASH earning the liquid-fund rate until the next rebalance
  - no re-entry until the next month-end, where the stock is treated normally

Everything else identical to dualmom_final.py, INCLUDING calendar-day liquid-fund
accrual -- an earlier version of this file accrued per trading day, which
understated the baseline by ~0.9 CAGR points. With stop=None this now reproduces
dualmom_final exactly.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dualmom_final import (load, load_nifty, allocate, LOOKBACK, SLIPPAGE_PCT,
                           LIQUID_FUND_PA, MAX_STALE_DAYS, RESULTS)

MRATE = (1 + LIQUID_FUND_PA) ** (1 / 12) - 1


def _grow(base, base_date, to_date):
    """Liquid-fund growth on calendar days -- same formula as dualmom_final."""
    return base * (1 + MRATE) ** ((to_date - base_date).days / 30.44)


def backtest(top_n=40, capital=1_000_000, stop=None):
    """stop=0.30 means exit when price <= entry*(1-0.30). None disables."""
    close = load("close")
    nifty, nifty_ma = load_nifty()
    ffill = close.ffill(limit=MAX_STALE_DAYS)
    month_ends = close.resample("ME").last().index

    cash_base = float(capital)
    base_date = month_ends[0]
    held, entry = {}, {}
    prev = month_ends[0]
    daily, stops = {}, []

    for rd in month_ends[1:]:
        i = close.index.get_indexer([rd], method="ffill")[0]
        if i < 0:
            continue
        rd = close.index[i]

        seg = ffill.loc[prev:rd]
        for d in seg.index:
            row = seg.loc[d]
            if stop is not None and held:
                for sym in list(held):
                    p = row.get(sym, np.nan)
                    if pd.isna(p) or p > entry[sym][0] * (1 - stop):
                        continue
                    # roll cash forward to today, then add the sale proceeds
                    cash_base = _grow(cash_base, base_date, d)
                    base_date = d
                    cash_base += held[sym] * p * (1 - SLIPPAGE_PCT)
                    stops.append({"date": d.date(), "sym": sym,
                                  "entry": round(entry[sym][0], 2),
                                  "exit": round(float(p), 2),
                                  "loss_pct": round((p / entry[sym][0] - 1) * 100, 1)})
                    del held[sym], entry[sym]
            daily[d] = _grow(cash_base, base_date, d) + sum(
                held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))

        cash = _grow(cash_base, base_date, rd)
        mark = ffill.iloc[i]
        lb = i - LOOKBACK
        if lb < 0:
            cash_base, base_date, prev = cash, rd, rd
            continue

        ni = nifty.index.get_indexer([rd], method="ffill")[0]
        up = (not pd.isna(nifty_ma.iloc[ni])) and nifty.iloc[ni] > nifty_ma.iloc[ni]
        r12 = (close.iloc[i] / close.iloc[lb] - 1).dropna()

        proceeds = cash
        for sym, sh in held.items():
            p = mark.get(sym, np.nan)
            if not pd.isna(p):
                proceeds += sh * p * (1 - SLIPPAGE_PCT)
        held, entry = {}, {}

        if up:
            top = r12.nlargest(top_n)
            raw = {s: max(v, 0.001) for s, v in top.items()}
            tot = sum(raw.values())
            w = {s: v / tot for s, v in raw.items()}
            eff = mark * (1 + SLIPPAGE_PCT)
            held = allocate(w, eff, proceeds)
            for s in held:
                entry[s] = (float(eff[s]), rd.date())
            cash_base = max(proceeds - sum(n * eff[s] for s, n in held.items()), 0)
        else:
            cash_base = proceeds
        base_date, prev = rd, rd

    seg = ffill.loc[prev:]
    for d in seg.index:
        row = seg.loc[d]
        daily[d] = _grow(cash_base, base_date, d) + sum(
            held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))

    nav = pd.Series(daily).sort_index()
    nav = nav[nav > 0]
    nm = nav.resample("ME").last().dropna()
    rm = nm.pct_change().dropna()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    dn = rm[rm < 0].std()
    return {
        "cagr": (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1,
        "sharpe": rm.mean() / rm.std() * np.sqrt(12),
        "sortino": rm.mean() / dn * np.sqrt(12) if dn > 0 else np.nan,
        "dd": (nav / nav.cummax() - 1).min(),
        "final": nav.iloc[-1],
        "n_stops": len(stops),
        "stops": pd.DataFrame(stops),
        "nav": nav,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stops", type=float, nargs="+", default=[0.50, 0.40, 0.35, 0.30, 0.25])
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    print("DualMom Top 40 — intra-month stop-loss, calendar-day accrual (matches canonical)\n")
    print("=" * 90)
    print(f"{'Variant':<24}{'CAGR':>9}{'Sharpe':>9}{'Sortino':>9}{'MaxDD':>10}"
          f"{'fired':>8}{'Final':>14}")
    print("=" * 90)

    base = backtest(40, 1_000_000, None)
    print(f"{'no stop (baseline)':<24}{base['cagr']*100:>8.2f}%{base['sharpe']:>9.3f}"
          f"{base['sortino']:>9.3f}{base['dd']*100:>9.1f}%{'-':>8}"
          f"{'  Rs %.2fCr' % (base['final']/1e7):>14}")

    keep = {}
    for s in a.stops:
        r = backtest(40, 1_000_000, s)
        keep[s] = r
        print(f"{'stop at -%d%%' % round(s*100):<24}{r['cagr']*100:>8.2f}%{r['sharpe']:>9.3f}"
              f"{r['sortino']:>9.3f}{r['dd']*100:>9.1f}%{r['n_stops']:>8}"
              f"{'  Rs %.2fCr' % (r['final']/1e7):>14}")
        if a.save and not r["stops"].empty:
            r["stops"].to_csv(RESULTS / f"stoploss_{round(s*100)}_trades.csv", index=False)
    print("=" * 90)

    r30 = keep.get(0.30)
    if r30 is not None and not r30["stops"].empty:
        st = r30["stops"].copy()
        st["year"] = pd.to_datetime(st["date"]).dt.year
        print(f"\n-30% STOP — fired {len(st)} times over {st.year.nunique()} distinct years\n")
        yr = st.groupby("year").size()
        full = pd.Series(0, index=range(2006, 2027))
        full.update(yr)
        print("firings by year:")
        for y, n in full.items():
            print(f"  {y}  {'#'*int(n)}{'' if n else '-'}  {int(n)}")
        print(f"\nlast 10 years (2017-2026): {int(full.loc[2017:].sum())} firings")
        print(f"first 10 years (2006-2016): {int(full.loc[:2016].sum())} firings")
        print(f"\n10 most recent firings:")
        print(st.tail(10)[["date", "sym", "entry", "exit", "loss_pct"]].to_string(index=False))
