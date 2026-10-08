"""
survivorship_test.py
Measure survivorship bias in DualMom.Liq.Nifty50 -- properly.

The problem: `nifty500_symbols.py` is TODAY's constituent list. Over half the
stocks trading in 2006 no longer exist. Several that died (VAKRANGEE, PCJEWELLER,
INFIBEAM, DHFL...) were top-40 momentum names immediately before they collapsed,
and the backtest has never been allowed to buy any of them.

Design -- the universe RULE is held constant, only the graveyard changes:

  Run A  point-in-time top-N by turnover, INCLUDING stocks that later died
  Run B  identical rule, but restricted to stocks still trading today

Both runs read the same bhavcopy matrix, so residual data-quality error is
common-mode and cancels in the difference.

  A - B  =  survivorship bias

Everything else matches `dualmom_final.py`: top 40 by 12m return, momentum
weighted, whole shares with the 2x cap, Nifty 50 100-day MA filter, 6% liquid fund.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

ROOT   = Path(r"G:\fyers_data_pipeline")
MATRIX = ROOT / "Bhavcopy" / "_matrix"
INDEX  = ROOT / "Nifty 500 Daily Fyers" / "_NIFTY50_INDEX.parquet"

LOOKBACK        = 252
UNIVERSE_N      = 500
TOP_N           = 40
TURNOVER_WIN    = 250
CAPITAL         = 1_000_000
SLIPPAGE_PCT    = 0.001
LIQUID_FUND_PA  = 0.06
START_DATE      = "2006-01-01"
MAX_STALE_DAYS  = 30
MAX_WEIGHT_MULT = 2.0


def allocate(weights, price, capital):
    afford = {s: w for s, w in weights.items()
              if s in price.index and price[s] > 0
              and price[s] <= capital * w * MAX_WEIGHT_MULT}
    if not afford:
        return {}
    tot = sum(afford.values())
    afford = {s: w / tot for s, w in afford.items()}
    q = {s: math.floor(capital * w / price[s]) for s, w in afford.items()}
    cash = capital - sum(q[s] * price[s] for s in q)
    for _ in range(2000):
        best, gap = None, 0.0
        for s, w in afford.items():
            p = price[s]
            if p > cash or (q[s] + 1) * p > capital * w * MAX_WEIGHT_MULT:
                continue
            short = capital * w - q[s] * p
            if short > gap:
                best, gap = s, short
        if best is None:
            break
        q[best] += 1
        cash -= price[best]
    return {s: n for s, n in q.items() if n > 0}


def run(close, value, nifty, nifty_ma, survivors_only, alive, label):
    ffill = close.ffill(limit=MAX_STALE_DAYS)
    turnover = value.rolling(TURNOVER_WIN, min_periods=60).mean()
    month_ends = close.resample("ME").last().index
    mrate = (1 + LIQUID_FUND_PA) ** (1 / 12) - 1

    cash, held = float(CAPITAL), {}
    prev = month_ends[0]
    daily, log = {}, []

    for rd in month_ends[1:]:
        i = close.index.get_indexer([rd], method="ffill")[0]
        if i < 0:
            continue
        rd = close.index[i]

        seg = ffill.loc[prev:rd]
        for d in seg.index:
            row = seg.loc[d]
            daily[d] = cash * (1 + mrate) ** ((d - prev).days / 30.44) + sum(
                held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))

        cash *= (1 + mrate) ** ((rd - prev).days / 30.44)
        mark = ffill.iloc[i]
        lb = i - LOOKBACK
        if lb < 0:
            prev = rd
            continue

        ni = nifty.index.get_indexer([rd], method="ffill")[0]
        up = (not pd.isna(nifty_ma.iloc[ni])) and nifty.iloc[ni] > nifty_ma.iloc[ni]

        # ---- point-in-time universe: most-traded names alive on this date ----
        tv = turnover.iloc[i].dropna()
        elig = close.iloc[i].dropna().index.intersection(close.iloc[lb].dropna().index)
        tv = tv.reindex(elig).dropna()
        if survivors_only:
            tv = tv[tv.index.isin(alive)]
        universe = tv.nlargest(UNIVERSE_N).index

        r12 = (close.iloc[i][universe] / close.iloc[lb][universe] - 1).dropna()

        proceeds = cash
        for sym, sh in held.items():
            p = mark.get(sym, np.nan)
            if not pd.isna(p):
                proceeds += sh * p * (1 - SLIPPAGE_PCT)
        held, cash = {}, proceeds

        if up and len(r12) >= TOP_N:
            top = r12.nlargest(TOP_N)
            raw = {s: max(v, 0.001) for s, v in top.items()}
            tot = sum(raw.values())
            w = {s: v / tot for s, v in raw.items()}
            eff = mark * (1 + SLIPPAGE_PCT)
            held = allocate(w, eff, proceeds)
            cash = max(proceeds - sum(n * eff[s] for s, n in held.items()), 0)
            log.append({"date": rd.date(), "signal": "IN", "universe": len(tv),
                        "names": len(held), "nav": round(proceeds, 2)})
        else:
            log.append({"date": rd.date(), "signal": "OUT", "universe": len(tv),
                        "names": 0, "nav": round(proceeds, 2)})
        prev = rd

    seg = ffill.loc[prev:]
    for d in seg.index:
        row = seg.loc[d]
        daily[d] = cash * (1 + mrate) ** ((d - prev).days / 30.44) + sum(
            held[s] * row[s] for s in held if s in row.index and not pd.isna(row[s]))

    nav = pd.Series(daily).sort_index()
    nav = nav[nav > 0]
    nm = nav.resample("ME").last().dropna()
    rm = nm.pct_change().dropna()
    yrs = (nav.index[-1] - nav.index[0]).days / 365.25
    return {
        "label": label,
        "cagr": (nav.iloc[-1] / nav.iloc[0]) ** (1 / yrs) - 1,
        "sharpe": rm.mean() / rm.std() * np.sqrt(12),
        "dd": (nav / nav.cummax() - 1).min(),
        "final": nav.iloc[-1],
        "nav": nav, "log": pd.DataFrame(log),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", action="store_true")
    args = ap.parse_args()

    print("loading bhavcopy matrix ...")
    close = pd.read_parquet(MATRIX / "close_adj.parquet").loc[START_DATE:]
    value = pd.read_parquet(MATRIX / "value.parquet").loc[START_DATE:]
    close = close.astype("float64")
    idx = pd.read_parquet(INDEX, columns=["close"])
    idx.index = pd.to_datetime(idx.index)
    nifty, nifty_ma = idx["close"], idx["close"].rolling(100).mean()

    alive = set(close.iloc[-1].dropna().index)
    print(f"  {close.shape[0]} days x {close.shape[1]} symbols | "
          f"alive today {len(alive)} | dead {close.shape[1]-len(alive)}")

    res = [
        run(close, value, nifty, nifty_ma, False, alive, "A  survivorship-FREE (incl. dead)"),
        run(close, value, nifty, nifty_ma, True,  alive, "B  survivors only (biased)"),
    ]

    print()
    print("=" * 78)
    print(f"{'Run':<36}{'CAGR':>9}{'Sharpe':>9}{'MaxDD':>9}{'Final':>14}")
    print("=" * 78)
    for r in res:
        print(f"{r['label']:<36}{r['cagr']*100:>8.2f}%{r['sharpe']:>9.3f}"
              f"{r['dd']*100:>8.1f}%{'  Rs %.2fCr' % (r['final']/1e7):>14}")
    print("=" * 78)
    a, b = res
    print(f"\nSURVIVORSHIP BIAS = B - A = {(b['cagr']-a['cagr'])*100:+.2f} CAGR points")
    print(f"  (the biased run overstates CAGR by this much)")
    print(f"  Sharpe {a['sharpe']:.3f} -> {b['sharpe']:.3f} | "
          f"MaxDD {a['dd']*100:.1f}% -> {b['dd']*100:.1f}%")

    if args.save:
        out = Path(__file__).parent / "results"
        out.mkdir(exist_ok=True)
        for r, n in zip(res, ("A_survivorship_free", "B_survivors_only")):
            r["nav"].to_csv(out / f"surv_{n}_nav.csv", header=["nav"])
            r["log"].to_csv(out / f"surv_{n}_log.csv", index=False)
        print(f"\nsaved -> {out}")


if __name__ == "__main__":
    main()
