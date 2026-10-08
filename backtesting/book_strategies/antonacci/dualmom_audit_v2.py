"""
dualmom_audit_v2.py
Corrected DualMom backtest + realism sensitivities.

Fixes three defects found reviewing the original scripts:
  1. Held stocks whose price was NaN at a rebalance had their entire value
     silently DELETED (OLECTRA 2018-01: 6.59% of NAV; FORCEMOT 2023-10: 1.97%).
     Now valued at last known price and carried until a real price returns.
  2. Drawdown was measured on MONTH-END NAV only, hiding everything that
     happened inside the month. Now marked daily.
  3. The Nifty filter read yfinance, which has no index data before mid-2007;
     pandas ffill then returned the LAST (2026) value for early dates. Now
     sourced from the Fyers index parquet, complete from 2005.

Sensitivities: execution at next-day open (what the live plan actually does)
vs the backtest's same-close fill.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path
import numpy as np
import pandas as pd

ROOT     = Path(r"G:\fyers_data_pipeline")
NEW_DIR  = ROOT / "Nifty 500 Daily Fyers"
INDEX_PQ = NEW_DIR / "_NIFTY50_INDEX.parquet"

LOOKBACK       = 252
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
MAX_STALE_DAYS = 30      # value a non-trading holding at its last price for up to this long


def load(field="close"):
    frames = {}
    for f in NEW_DIR.glob("*.parquet"):
        if f.stem.startswith("_"):
            continue
        d = pd.read_parquet(f, columns=[field])
        d.index = pd.to_datetime(d.index)
        frames[f.stem] = d[field]
    return pd.DataFrame(frames).sort_index().loc[START_DATE:]


def load_nifty():
    d = pd.read_parquet(INDEX_PQ, columns=["close"])
    d.index = pd.to_datetime(d.index)
    return d["close"], d["close"].rolling(100).mean()


def backtest(close, top_n, exec_px=None, fix_vanish=True):
    """exec_px: price matrix used to FILL orders (defaults to close = same-bar fill).
    Pass the next-day open matrix to model the live 'trade next morning' plan."""
    if exec_px is None:
        exec_px = close
    # last-known-price matrix, capped so a delisted name doesn't stay marked forever
    ffill = close.ffill(limit=MAX_STALE_DAYS) if fix_vanish else close

    month_ends = close.resample("ME").last().index
    mrate = (1 + LIQUID_FUND_PA) ** (1 / 12) - 1

    cash, held = CAPITAL, {}
    prev = month_ends[0]
    daily = {}
    nifty, nifty_ma = load_nifty()

    for rd in month_ends[1:]:
        i = close.index.get_indexer([rd], method="ffill")[0]
        if i < 0:
            continue
        rd = close.index[i]

        # daily mark-to-market for the month that just ended
        seg = ffill.loc[prev:rd]
        for d in seg.index:
            row = seg.loc[d]
            c_d = cash * (1 + mrate) ** ((d - prev).days / 30.44)
            daily[d] = c_d + sum(held[s] * row[s] for s in held
                                 if s in row.index and not pd.isna(row[s]))

        cash *= (1 + mrate) ** ((rd - prev).days / 30.44)
        mark = ffill.iloc[i]

        lb = i - LOOKBACK
        if lb < 0:
            prev = rd
            continue

        ni = nifty.index.get_indexer([rd], method="ffill")[0]
        up = (not pd.isna(nifty_ma.iloc[ni])) and nifty.iloc[ni] > nifty_ma.iloc[ni]

        # ranking always uses real closes, never forward-filled ones
        r12 = (close.iloc[i] / close.iloc[lb] - 1).dropna()
        cands = r12.nlargest(top_n).index.tolist() if up else []

        # ---- SELL: value at last known price, never delete the position ----
        fill = exec_px.iloc[i] if exec_px is not close else close.iloc[i]
        sell = cash
        for sym, sh in held.items():
            p = fill.get(sym, np.nan)
            if pd.isna(p):
                p = mark.get(sym, np.nan)          # fall back to last known
            if pd.isna(p):
                continue                            # genuinely no price ever
            sell += sh * p * (1 - SLIPPAGE_PCT)
        held, cash = {}, sell

        # ---- BUY ----
        if cands:
            raw = {s: max(r12[s], 0.001) for s in cands}
            tot = sum(raw.values())
            inv = 0.0
            for sym, v in raw.items():
                p = fill.get(sym, np.nan)
                if pd.isna(p):
                    p = mark.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = sell * (v / tot) * (1 + SLIPPAGE_PCT)
                held[sym] = cost / p
                inv += cost
            cash = max(sell - inv, 0)
        prev = rd

    # tail
    seg = ffill.loc[prev:]
    for d in seg.index:
        row = seg.loc[d]
        c_d = cash * (1 + mrate) ** ((d - prev).days / 30.44)
        daily[d] = c_d + sum(held[s] * row[s] for s in held
                             if s in row.index and not pd.isna(row[s]))

    nav_d = pd.Series(daily).sort_index()
    nav_d = nav_d[nav_d > 0]
    nav_m = nav_d.resample("ME").last().dropna()

    rm = nav_m.pct_change().dropna()
    yrs = (nav_d.index[-1] - nav_d.index[0]).days / 365.25
    return {
        "cagr":    (nav_d.iloc[-1] / nav_d.iloc[0]) ** (1 / yrs) - 1,
        "sharpe":  rm.mean() / rm.std() * np.sqrt(12) if rm.std() > 0 else 0.0,
        "dd_daily":   (nav_d / nav_d.cummax() - 1).min(),
        "dd_monthly": (nav_m / nav_m.cummax() - 1).min(),
        "final":   nav_d.iloc[-1],
        "nav_d":   nav_d,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, nargs="+", default=[40, 50])
    args = ap.parse_args()

    close = load("close")
    open_ = load("open")
    # execution at the NEXT session's open = shift the open matrix back one row
    nxt_open = open_.reindex(close.index).shift(-1)

    print(f"Universe {close.shape[1]} symbols | {close.index[0].date()} -> {close.index[-1].date()}\n")
    print("=" * 84)
    print(f"{'Variant':<40}{'CAGR':>9}{'Sharpe':>9}{'DD(mth)':>10}{'DD(daily)':>11}{'Final':>12}")
    print("=" * 84)

    for t in args.top:
        rows = [
            (f"Top {t} — as originally measured", backtest(close, t, fix_vanish=False)),
            (f"Top {t} — vanish bug fixed",       backtest(close, t)),
            (f"Top {t} — fixed + next-day-open fill", backtest(close, t, exec_px=nxt_open)),
        ]
        for label, r in rows:
            print(f"{label:<40}{r['cagr']*100:>8.2f}%{r['sharpe']:>9.3f}"
                  f"{r['dd_monthly']*100:>9.1f}%{r['dd_daily']*100:>10.1f}%"
                  f"{'  Rs %.1fCr' % (r['final']/1e7):>12}")
        print("-" * 84)


if __name__ == "__main__":
    main()
