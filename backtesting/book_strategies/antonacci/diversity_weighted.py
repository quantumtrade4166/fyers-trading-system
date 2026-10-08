"""
Diversity-Weighted Portfolio — Fernholz et al. (2005)
Tests 5 configs vs DualMom baseline:

  A) BASELINE     — DualMom V7 (top 50 momentum weighted)          ← our current best
  B) STANDALONE   — Diversity all 500 (no stock picking, p=0.5)
  C) MERGE-1      — Top 50 momentum selection + diversity allocation within 50
  D) MERGE-2      — Top 50 momentum selection + combined weight (momentum × diversity)
  E) MERGE-3      — Diversity allocation across all 500, but with 100MA filter (OUT=liquid fund)

Market cap proxy: avg daily turnover (price × volume) — better than price alone.
Power p = 0.5 throughout (square-root weighting). Sweep p at end.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

DATA_DIR       = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS  = 252
TOP_N          = 50
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"
P_POWER        = 0.5          # diversity exponent

print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols (close + volume)...")
close_frames  = {}
volume_frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close", "volume"])
    df.index = pd.to_datetime(df.index)
    close_frames[f.stem]  = df["close"]
    volume_frames[f.stem] = df["volume"]

prices  = pd.DataFrame(close_frames).sort_index().loc[START_DATE:END_DATE]
volumes = pd.DataFrame(volume_frames).sort_index().loc[START_DATE:END_DATE]

# Market cap proxy: 20-day avg turnover (price × volume)
print("Computing turnover proxy for market cap...")
turnover       = prices * volumes
turnover_ma20  = turnover.rolling(20).mean()

monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days × {prices.shape[1]} symbols\n")


def diversity_weights(syms, turnover_now, p=P_POWER):
    """Diversity-weighted allocation: w_i = cap_i^p / sum(cap_j^p)"""
    caps = {s: turnover_now.get(s, np.nan) for s in syms}
    caps = {s: v for s, v in caps.items() if not pd.isna(v) and v > 0}
    if not caps:
        return {s: 1/len(syms) for s in syms}
    powered = {s: v ** p for s, v in caps.items()}
    total   = sum(powered.values())
    return {s: v / total for s, v in powered.items()}


def momentum_weights(syms, returns_12m):
    """Momentum-weighted: w_i ∝ max(return_i, 0.001)"""
    raw   = {s: max(returns_12m.get(s, 0.001), 0.001) for s in syms}
    total = sum(raw.values())
    return {s: v / total for s, v in raw.items()}


def combined_weights(syms, returns_12m, turnover_now, p=P_POWER):
    """Combined: w_i ∝ momentum_i × diversity_i (then renorm)"""
    mom  = momentum_weights(syms, returns_12m)
    div  = diversity_weights(syms, turnover_now, p)
    raw  = {s: mom[s] * div.get(s, 1e-9) for s in syms}
    total = sum(raw.values())
    if total <= 0:
        return {s: 1/len(syms) for s in syms}
    return {s: v / total for s, v in raw.items()}


def run_backtest(config, label):
    portfolio_value = [CAPITAL]
    dates           = [monthly_ends[0]]
    cash_value      = CAPITAL
    held_stocks     = {}
    cash_months     = 0
    prev_date       = monthly_ends[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]
        turn_now   = turnover_ma20.iloc[idx].to_dict()

        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        nav = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                nav += shares * p

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            portfolio_value.append(nav)
            dates.append(rebal_date)
            prev_date = rebal_date
            continue

        # absolute filter (100MA)
        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        if not market_up:
            cash_months += 1

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna().to_dict()

        # ---- determine candidates and weights per config ----
        if config == "baseline":
            # top 50 by momentum, momentum weighted
            top50 = sorted(returns_12m, key=returns_12m.get, reverse=True)[:TOP_N]
            candidates = top50 if market_up else []
            weights    = momentum_weights(candidates, returns_12m) if candidates else {}

        elif config == "standalone":
            # ALL stocks, diversity weighted — always invested (no 100MA filter)
            all_syms   = [s for s in current_px.index if not pd.isna(current_px[s]) and current_px[s] > 0]
            candidates = all_syms
            weights    = diversity_weights(candidates, turn_now)

        elif config == "merge1":
            # top 50 momentum selection + diversity allocation within those 50
            top50      = sorted(returns_12m, key=returns_12m.get, reverse=True)[:TOP_N]
            candidates = top50 if market_up else []
            weights    = diversity_weights(candidates, turn_now) if candidates else {}

        elif config == "merge2":
            # top 50 momentum selection + combined (momentum × diversity) allocation
            top50      = sorted(returns_12m, key=returns_12m.get, reverse=True)[:TOP_N]
            candidates = top50 if market_up else []
            weights    = combined_weights(candidates, returns_12m, turn_now) if candidates else {}

        elif config == "merge3":
            # diversity across ALL 500, but 100MA filter applies (OUT = liquid fund)
            if market_up:
                all_syms   = [s for s in current_px.index if not pd.isna(current_px[s]) and current_px[s] > 0]
                candidates = all_syms
                weights    = diversity_weights(candidates, turn_now)
            else:
                candidates = []
                weights    = {}

        # sell everything
        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        held_stocks = {}
        cash_value  = sell_value

        if candidates and weights:
            invested = 0
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = sell_value * w * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        portfolio_value.append(nav)
        dates.append(rebal_date)
        prev_date = rebal_date

    final_nav = cash_value
    for sym, shares in held_stocks.items():
        if sym in prices.columns:
            final_nav += shares * prices[sym].dropna().iloc[-1]
    portfolio_value.append(final_nav)
    dates.append(prices.index[-1])

    nav_s     = pd.Series(portfolio_value, index=dates)
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {
        "label":      label,
        "cagr":       cagr,
        "sharpe":     sharpe,
        "max_dd":     max_dd,
        "final_nav":  nav_s.iloc[-1],
        "cash_months": cash_months,
        "nav_series": nav_s,
    }


configs = [
    ("baseline",   "A) DualMom baseline (momentum wt)"),
    ("standalone", "B) Standalone diversity (all 500)"),
    ("merge1",     "C) Merge-1: top50 + diversity alloc"),
    ("merge2",     "D) Merge-2: top50 + combined weight"),
    ("merge3",     "E) Merge-3: diversity all500 + 100MA"),
]

results = []
for cfg, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(cfg, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.1f}%  NAV=Rs {r['final_nav']/1e7:.1f}Cr")

print("\n" + "=" * 84)
print("  DIVERSITY-WEIGHTED vs DualMom BASELINE  (p=0.5, monthly rebalance)")
print("=" * 84)
print(f"  {'Config':<38} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>12} {'CashMths':>9}")
print(f"  {'-'*38} {'-'*7} {'-'*8} {'-'*8} {'-'*12} {'-'*9}")
for r in results:
    print(f"  {r['label']:<38} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.1f}% "
          f"  Rs{r['final_nav']/1e7:>6.1f}Cr {r['cash_months']:>5}m")
print("=" * 84)

# year by year
print("\n  Year-by-year returns:")
header = f"  {'Year':<6}" + "".join(f" {'ABCDE'[i]:>9}" for i in range(len(results)))
print(header)
print("  " + "-" * (6 + 10 * len(results)))
navs = [r["nav_series"].resample("YE").last() for r in results]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    row = f"  {yr:<6}"
    for nav in navs:
        yr_idx = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yr_idx and yr_idx[0] > 0:
            ret = (nav.iloc[yr_idx[0]] / nav.iloc[yr_idx[0]-1] - 1) * 100
            row += f" {ret:>8.1f}%"
        else:
            row += f" {'—':>9}"
    print(row)

# p-power sweep on standalone
print("\n  P-power sweep (standalone diversity, all 500, no 100MA filter):")
print(f"  {'p':>6} {'CAGR':>8} {'Sharpe':>8} {'MaxDD':>8}")
print(f"  {'-'*6} {'-'*8} {'-'*8} {'-'*8}")
for p_val in [0.1, 0.25, 0.5, 0.75, 1.0]:
    # quick sweep: reuse same structure but with different p
    nav_val = CAPITAL
    cash_   = CAPITAL
    held_   = {}
    prev_   = monthly_ends[0]
    navs_p  = [CAPITAL]
    dts_p   = [monthly_ends[0]]

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0: continue
        rebal_date = prices.index[idx]
        cur_px  = prices.iloc[idx]
        turn_n  = turnover_ma20.iloc[idx].to_dict()
        me      = (rebal_date - prev_).days / 30.44
        cash_  *= (1 + monthly_rate) ** me
        nav_    = cash_ + sum(held_.get(s, 0) * cur_px.get(s, 0) for s in held_)
        lb_idx  = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            navs_p.append(nav_); dts_p.append(rebal_date); prev_ = rebal_date; continue
        all_syms = [s for s in cur_px.index if not pd.isna(cur_px[s]) and cur_px[s] > 0]
        wts     = diversity_weights(all_syms, turn_n, p=p_val)
        sv      = cash_ + sum(held_.get(s, 0) * cur_px.get(s, 0) * (1 - SLIPPAGE_PCT) for s in held_)
        held_   = {}
        cash_   = sv
        inv     = 0
        for sym, w in wts.items():
            p_ = cur_px.get(sym, np.nan)
            if pd.isna(p_) or p_ <= 0: continue
            cost = sv * w * (1 + SLIPPAGE_PCT)
            held_[sym] = cost / p_
            inv += cost
        cash_ = max(sv - inv, 0)
        navs_p.append(nav_); dts_p.append(rebal_date); prev_ = rebal_date

    fn  = cash_ + sum(held_.get(s, 0) * prices[s].dropna().iloc[-1] for s in held_ if s in prices.columns)
    nav_s = pd.Series(navs_p + [fn], index=dts_p + [prices.index[-1]])
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")]
    rm    = nav_s.pct_change().dropna()
    ny    = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cg    = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1/ny) - 1
    sh    = rm.mean() / rm.std() * np.sqrt(12) if rm.std() > 0 else 0
    md    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()
    print(f"  {p_val:>6.2f} {cg*100:>7.2f}% {sh:>8.3f} {md*100:>7.1f}%")

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results}).to_csv(out_dir / "diversity_weighted_nav.csv")
print(f"\n  NAV saved → {out_dir / 'diversity_weighted_nav.csv'}")
