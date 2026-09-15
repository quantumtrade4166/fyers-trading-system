"""
dualmom_final.py - CANONICAL DualMom.Liq.Nifty50 backtest.

Supersedes dual_momentum_v7.py, top40_vs_top50.py and is_oos_test.py, all of
which carry the defects listed below. Numbers from THIS file are the ones that
belong in the vault.

Defects fixed vs the earlier scripts
------------------------------------
1. Nifty filter read yfinance, which has no index data before mid-2007. pandas
   get_indexer(method="ffill") returns -1 for dates before a series starts, and
   .iloc[-1] then handed back the 2026 value, so the trend filter was reading
   the future for 2006 to Jun-2007. It then sat in cash through the whole
   H2-2007 rally because the 100-day average had too little data. Now uses the
   Fyers index parquet, complete from 2005.
2. Stock prices were not corporate-action adjusted (94 fake crashes over 84 of
   500 names). Rebuilt from Fyers History, which back-adjusts (11 events remain).
3. A held stock with no price on rebalance day had its ENTIRE value deleted
   (OLECTRA 2018-01 = 6.6% of NAV; FORCEMOT 2023-10 = 2.0%). Now valued at its
   last known price.
4. Slippage was applied as shares = alloc*(1+slip)/price instead of
   alloc/(price*(1+slip)), buying about 0.2% too many shares EVERY rebalance and
   compounding to roughly +2 CAGR points of fiction over 154 rebalances.
5. Drawdown was measured on month-end NAV only. Now marked daily.
6. Fractional shares. Indian equities are whole-share only.

Live allocation rules (agreed 2026-09-01)
-----------------------------------------
- whole shares only
- a name unaffordable at its target weight is skipped and its weight
  redistributed across the rest, never left as idle cash
- leftover rupees go to whichever holding sits furthest below target
- never buy a share that would push a position past MAX_WEIGHT_MULT x target
- ADOPTED 2026-09-02: intra-month stop at -35% from entry. Sell on the close,
  park the proceeds in cash until the next rebalance, then treat the stock
  normally again. Costs 0.17 CAGR points, cuts daily drawdown -31.7% -> -30.3%.
  Fires 28 times in 20.6 years. Set STOP_LOSS = None to disable.
- ADOPTED 2026-09-08: MAX_WEIGHT = 10%. Momentum weighting is otherwise uncapped
  and put 35.9% of the book into PATANJALI in Jan-2021. Excess weight is
  redistributed pro-rata to the uncapped names, iterating until nothing exceeds
  the cap. Set MAX_WEIGHT = None to disable.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT     = Path(r"G:\fyers_data_pipeline")
DATA_DIR = ROOT / "Nifty 500 Daily Fyers"
INDEX_PQ = DATA_DIR / "_NIFTY50_INDEX.parquet"
RESULTS  = Path(__file__).parent / "results"

LOOKBACK        = 252
SLIPPAGE_PCT    = 0.001
LIQUID_FUND_PA  = 0.06
START_DATE      = "2006-01-01"
MAX_STALE_DAYS  = 30
MAX_WEIGHT_MULT = 2.0        # never let one share push a position past 2x target
STOP_LOSS       = 0.35       # ADOPTED 2026-09-02: exit intra-month at -35% from entry
MAX_WEIGHT      = 0.10       # ADOPTED 2026-09-08: no position above 10% of NAV
USE_MA_FILTER   = True       # False = always invested (for measuring the filter)       # ADOPTED 2026-09-08: no position above 10% of NAV


def load(field="close", data_start=None):
    frames = {}
    for f in DATA_DIR.glob("*.parquet"):
        if f.stem.startswith("_"):
            continue
        d = pd.read_parquet(f, columns=[field])
        d.index = pd.to_datetime(d.index)
        frames[f.stem] = d[field]
    return pd.DataFrame(frames).sort_index().loc[data_start or START_DATE:]


def load_nifty():
    d = pd.read_parquet(INDEX_PQ, columns=["close"])
    d.index = pd.to_datetime(d.index)
    return d["close"], d["close"].rolling(100).mean()


def cap_weights(raw: dict, wcap: float) -> dict:
    """Cap every weight at `wcap`, redistributing the excess pro-rata to the
    names still under it. Iterates because redistribution can push a previously
    uncapped name over.

    The total ALWAYS stays exactly 1.0. An earlier version added the excess to
    names already sitting at the cap, which inflated the total above 1.0 and
    silently levered the book -- it showed up as CAGR rising while Sharpe
    collapsed. Guard with the assert in the caller.
    """
    if not raw:
        return {}
    tot = sum(raw.values())
    base = {s: v / tot for s, v in raw.items()}
    if wcap is None or wcap >= 1.0:
        return base
    if wcap * len(base) <= 1.0:               # cap unreachable -> equal weight
        return {s: 1.0 / len(base) for s in base}

    capped = set()
    while True:
        others = [s for s in base if s not in capped]
        remaining = 1.0 - len(capped) * wcap
        if not others or remaining <= 0:
            return {s: 1.0 / len(base) for s in base}
        sub = sum(base[s] for s in others)
        w = {s: wcap for s in capped}
        newly = []
        for s in others:
            w[s] = remaining * base[s] / sub
            if w[s] > wcap + 1e-12:
                newly.append(s)
        if not newly:
            return w
        capped.update(newly)


def allocate(weights, price, capital):
    """Whole-share allocation -> {symbol: shares}.

    Skip-and-redistribute, greedy fill of the remainder, 2x target-weight ceiling.
    `price` must already include the slippage uplift.
    """
    afford = {s: w for s, w in weights.items()
              if s in price.index and price[s] > 0
              and price[s] <= capital * w * MAX_WEIGHT_MULT}
    if not afford:
        return {}
    tot = sum(afford.values())
    afford = {s: w / tot for s, w in afford.items()}

    q = {s: math.floor(capital * w / price[s]) for s, w in afford.items()}
    cash = capital - sum(q[s] * price[s] for s in q)

    for _ in range(2000):                  # spend the remainder where it helps most
        best, gap = None, 0.0
        for s, w in afford.items():
            p = price[s]
            if p > cash:
                continue
            if (q[s] + 1) * p > capital * w * MAX_WEIGHT_MULT:
                continue
            short = capital * w - q[s] * p
            if short > gap:
                best, gap = s, short
        if best is None:
            break
        q[best] += 1
        cash -= price[best]
    return {s: n for s, n in q.items() if n > 0}


MRATE = (1 + LIQUID_FUND_PA) ** (1 / 12) - 1


def _grow(base, base_date, to_date):
    """Liquid-fund growth over calendar days."""
    return base * (1 + MRATE) ** ((to_date - base_date).days / 30.44)


def backtest(top_n=40, capital=1_000_000, whole=True, stop=STOP_LOSS,
             start=None, end=None, data_start=None):
    """start/end bound the TRADING window; data_start controls how much history is
    loaded for the 252-day lookback (defaults to START_DATE). Used by the IS/OOS test."""
    close = load("close", data_start=data_start)
    nifty, nifty_ma = load_nifty()
    ffill = close.ffill(limit=MAX_STALE_DAYS)
    month_ends = close.resample("ME").last().index
    if start is not None:
        keep = month_ends[month_ends >= pd.Timestamp(start)]
        before = month_ends[month_ends < pd.Timestamp(start)]
        anchor = before[-1:]          # ONE prior month-end to seed prev/base_date
        month_ends = anchor.union(keep)
    if end is not None:
        month_ends = month_ends[month_ends <= pd.Timestamp(end)]

    cash_base = float(capital)
    base_date = month_ends[0]
    held, entry = {}, {}
    prev = month_ends[0]
    daily, log, stops = {}, [], []

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
        if not USE_MA_FILTER:
            up = True            # never step aside — hold through every crash
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
            w = cap_weights(raw, MAX_WEIGHT)
            assert abs(sum(w.values()) - 1.0) < 1e-9, sum(w.values())
            eff = mark * (1 + SLIPPAGE_PCT)
            if whole:
                held = allocate(w, eff, proceeds)
            else:
                held = {s: proceeds * wt / eff[s] for s, wt in w.items()
                        if s in eff.index and eff[s] > 0}
            for s in held:
                entry[s] = (float(eff[s]), rd.date())
            cash_base = max(proceeds - sum(n * eff[s] for s, n in held.items()), 0)
            log.append({"date": rd.date(), "signal": "IN", "names": len(held),
                        "idle_cash_pct": round(cash_base / proceeds * 100, 3),
                        "nav": round(proceeds, 2)})
        else:
            cash_base = proceeds
            log.append({"date": rd.date(), "signal": "OUT", "names": 0,
                        "idle_cash_pct": 100.0, "nav": round(proceeds, 2)})
        base_date, prev = rd, rd

    seg = ffill.loc[prev:pd.Timestamp(end)] if end is not None else ffill.loc[prev:]
    for d in seg.index:
        row = seg.loc[d]
        daily[d] = _grow(cash_base, base_date, d) + sum(
            held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))

    nav = pd.Series(daily).sort_index()
    nav = nav[nav > 0]
    nm = nav.resample("ME").last().dropna()
    rm = nm.pct_change().dropna()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    dd_d = nav / nav.cummax() - 1
    dn = rm[rm < 0].std()
    return {
        "cagr": (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1,
        "sharpe": rm.mean() / rm.std() * np.sqrt(12),
        "sortino": rm.mean() / dn * np.sqrt(12) if dn > 0 else np.nan,
        "dd_daily": dd_d.min(), "dd_date": dd_d.idxmin(),
        "dd_monthly": (nm / nm.cummax() - 1).min(),
        "final": nav.iloc[-1], "years": yrs,
        "win_rate": (rm > 0).mean() * 100,
        "months_in": sum(1 for r in log if r["signal"] == "IN"),
        "months_out": sum(1 for r in log if r["signal"] == "OUT"),
        "n_stops": len(stops),
        "nav": nav, "log": pd.DataFrame(log), "stops": pd.DataFrame(stops),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=1_000_000)
    ap.add_argument("--top", type=int, nargs="+", default=[40, 50])
    ap.add_argument("--save", action="store_true")
    a = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)

    print(f"DualMom.Liq.Nifty50 - canonical backtest   capital Rs {a.capital:,.0f}\n")
    print("=" * 98)
    print(f"{'Variant':<32}{'CAGR':>8}{'Sharpe':>8}{'Sortino':>9}{'DD mth':>9}"
          f"{'DD daily':>10}{'Win%':>7}{'IN/OUT':>10}{'Final':>13}")
    print("=" * 98)
    for t in a.top:
        for whole, lbl in ((False, "fractional (theoretical)"), (True, "WHOLE shares (live)")):
            r = backtest(t, a.capital, whole)
            print(f"{'Top %d  %s' % (t, lbl):<32}{r['cagr']*100:>7.2f}%{r['sharpe']:>8.3f}"
                  f"{r['sortino']:>9.3f}{r['dd_monthly']*100:>8.1f}%{r['dd_daily']*100:>9.1f}%"
                  f"{r['win_rate']:>6.1f}%{'%d/%d' % (r['months_in'], r['months_out']):>10}"
                  f"{'  Rs %.2fCr' % (r['final']/1e7):>13}")
            if a.save and whole:
                r["nav"].to_csv(RESULTS / f"final_nav_top{t}.csv", header=["nav"])
                r["log"].to_csv(RESULTS / f"final_rebalance_log_top{t}.csv", index=False)
                print(f"     worst daily drawdown {r['dd_daily']*100:.1f}% on {r['dd_date'].date()}"
                      f" | {r['years']:.1f} years")
        print("-" * 98)
    if a.save:
        print(f"\nsaved -> {RESULTS}")
