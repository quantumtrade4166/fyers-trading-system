"""
compare_adjusted_vs_raw.py
How much did the unadjusted price data distort the DualMom backtest?

Runs the identical strategy over two stock datasets:
  OLD = "Nifty 500 Daily Data"   -- NOT corporate-action adjusted (94 suspected
                                     split/bonus/demerger events over 84 symbols)
  NEW = "Nifty 500 Daily Fyers"  -- rebuilt from Fyers History, which back-adjusts

The Nifty 50 filter comes from the SAME Fyers index file in both runs, so the
stock price data is the only variable. Symbols are restricted to the set common
to both datasets so coverage differences cannot explain the gap either.

Usage:  python compare_adjusted_vs_raw.py [--top 40 50] [--full-new]
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT      = Path(r"G:\fyers_data_pipeline")
OLD_DIR   = ROOT / "Nifty 500 Daily Data"
NEW_DIR   = ROOT / "Nifty 500 Daily Fyers"
INDEX_PQ  = NEW_DIR / "_NIFTY50_INDEX.parquet"

LOOKBACK_DAYS  = 252
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"      # old dataset ends here; keeps the comparison fair


def load_prices(data_dir: Path, only=None, end=END_DATE) -> pd.DataFrame:
    frames = {}
    for f in data_dir.glob("*.parquet"):
        if f.stem.startswith("_"):
            continue                      # index file
        if only is not None and f.stem not in only:
            continue
        d = pd.read_parquet(f, columns=["close"])
        d.index = pd.to_datetime(d.index)
        frames[f.stem] = d["close"]
    return pd.DataFrame(frames).sort_index().loc[START_DATE:end]


def load_nifty():
    d = pd.read_parquet(INDEX_PQ, columns=["close"])
    d.index = pd.to_datetime(d.index)
    n = d["close"]
    return n, n.rolling(100).mean()


def run(prices, nifty, nifty_ma, top_n):
    monthly_ends = prices.resample("ME").last().index
    monthly_rate = (1 + LIQUID_FUND_PA) ** (1 / 12) - 1

    cash_value, held = CAPITAL, {}
    prev_date = monthly_ends[0]
    nav_series = {monthly_ends[0]: CAPITAL}

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        cash_value *= (1 + monthly_rate) ** ((rebal_date - prev_date).days / 30.44)
        nav = cash_value + sum(
            held[s] * current_px.get(s, 0)
            for s in held if not pd.isna(current_px.get(s, np.nan))
        )

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            nav_series[rebal_date] = nav
            prev_date = rebal_date
            continue

        n_i = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma, n_px = nifty_ma.iloc[n_i], nifty.iloc[n_i]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        returns_12m = (current_px / prices.iloc[lb_idx] - 1).dropna()
        candidates = returns_12m.nlargest(top_n).index.tolist() if market_up else []

        sell_value = cash_value
        for sym, sh in held.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += sh * p * (1 - SLIPPAGE_PCT)
        held, cash_value = {}, sell_value

        if candidates:
            raw = {s: max(returns_12m[s], 0.001) for s in candidates}
            tot = sum(raw.values())
            invested = 0.0
            for sym, v in raw.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = sell_value * (v / tot) * (1 + SLIPPAGE_PCT)
                held[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    final = cash_value + sum(held[s] * prices[s].dropna().iloc[-1]
                             for s in held if s in prices.columns)
    nav_s = pd.Series(nav_series)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")].sort_index()
    nav_s[prices.index[-1]] = final

    rm = nav_s.pct_change().dropna()
    yrs = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    dd = (nav_s / nav_s.cummax() - 1)
    return {
        "cagr":   (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / yrs) - 1,
        "sharpe": rm.mean() / rm.std() * np.sqrt(12) if rm.std() > 0 else 0.0,
        "maxdd":  dd.min(),
        "final":  nav_s.iloc[-1],
        "nav":    nav_s,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, nargs="+", default=[40, 50])
    ap.add_argument("--full-new", action="store_true",
                    help="also run NEW data to its true end date, all symbols")
    args = ap.parse_args()

    nifty, nifty_ma = load_nifty()
    print(f"Nifty 50 (Fyers): {nifty.index[0].date()} -> {nifty.index[-1].date()}  "
          f"{len(nifty)} bars\n")

    old_syms = {f.stem for f in OLD_DIR.glob("*.parquet")}
    new_syms = {f.stem for f in NEW_DIR.glob("*.parquet") if not f.stem.startswith("_")}
    common = old_syms & new_syms
    print(f"Symbols: OLD {len(old_syms)} | NEW {len(new_syms)} | common {len(common)}")
    print("Comparison uses the COMMON set so only price adjustment differs.\n")

    old_px = load_prices(OLD_DIR, only=common)
    new_px = load_prices(NEW_DIR, only=common)
    print(f"OLD matrix {old_px.shape}   NEW matrix {new_px.shape}\n")

    print("=" * 74)
    print(f"{'Dataset':<26}{'TopN':>6}{'CAGR':>10}{'Sharpe':>10}{'MaxDD':>10}{'Final NAV':>14}")
    print("=" * 74)
    results = {}
    for top_n in args.top:
        for label, px in (("OLD (unadjusted)", old_px), ("NEW (Fyers adjusted)", new_px)):
            r = run(px, nifty, nifty_ma, top_n)
            results[(label, top_n)] = r
            print(f"{label:<26}{top_n:>6}{r['cagr']*100:>9.2f}%{r['sharpe']:>10.3f}"
                  f"{r['maxdd']*100:>9.1f}%{'  Rs %.2f Cr' % (r['final']/1e7):>14}")
        print("-" * 74)

    print("\nDifference introduced by unadjusted data:")
    for top_n in args.top:
        o = results[("OLD (unadjusted)", top_n)]
        n = results[("NEW (Fyers adjusted)", top_n)]
        print(f"  Top {top_n}:  CAGR {o['cagr']*100:.2f}% -> {n['cagr']*100:.2f}%  "
              f"({(n['cagr']-o['cagr'])*100:+.2f} pts)   "
              f"Sharpe {o['sharpe']:.3f} -> {n['sharpe']:.3f}   "
              f"NAV Rs {o['final']/1e7:.2f} Cr -> Rs {n['final']/1e7:.2f} Cr")

    if args.full_new:
        print("\n" + "=" * 74)
        print("NEW data, full coverage, to latest available date")
        print("=" * 74)
        full = load_prices(NEW_DIR, only=None, end="2026-12-31")
        print(f"Matrix {full.shape}  -> {full.index[-1].date()}\n")
        for top_n in args.top:
            r = run(full, nifty, nifty_ma, top_n)
            print(f"  Top {top_n}:  CAGR {r['cagr']*100:6.2f}%   Sharpe {r['sharpe']:.3f}   "
                  f"MaxDD {r['maxdd']*100:6.1f}%   Rs {r['final']/1e7:.2f} Cr")


if __name__ == "__main__":
    main()
