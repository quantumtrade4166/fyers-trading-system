"""
DualMom — Hold Until Exit vs Monthly Rebalance Comparison

A) Monthly Rebalance (current V7)  — sell/rebuild top 50 every month-end
B) Hold Until Exit                 — buy top 50 on OUT->IN cross, hold frozen
                                     until IN->OUT cross, then liquid fund

Key difference: B trades only at regime transitions (~2x per IN streak avg)
vs A which trades every month-end (12x per year).
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

print("Downloading Nifty 50...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty     = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]

monthly_ends = prices.resample("ME").last().index
monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Matrix: {prices.shape}\n")


def momentum_weights(candidates, returns_12m):
    raw   = {s: max(returns_12m.get(s, 0.001), 0.001) for s in candidates}
    total = sum(raw.values())
    return {s: v / total for s, v in raw.items()}


# ── A) Monthly Rebalance (baseline V7) ────────────────────────────────────────
def run_monthly_rebalance():
    cash_value  = CAPITAL
    held_stocks = {}
    prev_date   = monthly_ends[0]
    nav_series  = {monthly_ends[0]: CAPITAL}
    n_trades    = 0

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0: continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        nav = cash_value + sum(
            held_stocks.get(s, 0) * current_px.get(s, 0)
            for s in held_stocks if not pd.isna(current_px.get(s, np.nan))
        )

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            nav_series[rebal_date] = nav
            prev_date = rebal_date
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna().to_dict()
        candidates  = sorted(returns_12m, key=returns_12m.get, reverse=True)[:TOP_N] if market_up else []

        # sell all
        sell_value = cash_value
        if held_stocks:
            n_trades += len(held_stocks)
            for sym, shares in held_stocks.items():
                p = current_px.get(sym, np.nan)
                if not pd.isna(p):
                    sell_value += shares * p * (1 - SLIPPAGE_PCT)
        held_stocks = {}
        cash_value  = sell_value

        if candidates:
            weights  = momentum_weights(candidates, returns_12m)
            invested = 0
            n_trades += len(weights)
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0: continue
                cost = sell_value * w * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    final_nav = cash_value + sum(
        held_stocks.get(s, 0) * prices[s].dropna().iloc[-1]
        for s in held_stocks if s in prices.columns
    )
    nav_s = pd.Series(nav_series)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")].sort_index()
    return nav_s, n_trades


# ── B) Hold Until Exit ────────────────────────────────────────────────────────
def run_hold_until_exit():
    cash_value  = CAPITAL
    held_stocks = {}
    prev_date   = monthly_ends[0]
    nav_series  = {monthly_ends[0]: CAPITAL}
    prev_signal = None   # track last month's signal to detect transitions
    n_trades    = 0
    cash_months = 0

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0: continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        nav = cash_value + sum(
            held_stocks.get(s, 0) * current_px.get(s, 0)
            for s in held_stocks if not pd.isna(current_px.get(s, np.nan))
        )

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            nav_series[rebal_date] = nav
            prev_date = rebal_date
            continue

        nifty_idx  = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma       = nifty_ma100.iloc[nifty_idx]
        n_px       = nifty.iloc[nifty_idx]
        market_up  = (not pd.isna(n_ma)) and (n_px > n_ma)
        cur_signal = "IN" if market_up else "OUT"

        if not market_up:
            cash_months += 1

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna().to_dict()

        # only act on TRANSITIONS
        if prev_signal == "OUT" and cur_signal == "IN":
            # OUT -> IN: buy top 50, hold frozen
            candidates = sorted(returns_12m, key=returns_12m.get, reverse=True)[:TOP_N]
            sell_value = cash_value   # no stocks held when coming from OUT
            weights    = momentum_weights(candidates, returns_12m)
            invested   = 0
            n_trades  += len(weights)
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0: continue
                cost = sell_value * w * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        elif prev_signal == "IN" and cur_signal == "OUT":
            # IN -> OUT: sell everything, go to liquid fund
            sell_value = cash_value
            n_trades  += len(held_stocks)
            for sym, shares in held_stocks.items():
                p = current_px.get(sym, np.nan)
                if not pd.isna(p):
                    sell_value += shares * p * (1 - SLIPPAGE_PCT)
            held_stocks = {}
            cash_value  = sell_value

        # if IN->IN or OUT->OUT: do nothing (hold or stay in liquid fund)

        nav_series[rebal_date] = nav
        prev_date   = prev_date
        prev_signal = cur_signal
        prev_date   = rebal_date

    final_nav = cash_value + sum(
        held_stocks.get(s, 0) * prices[s].dropna().iloc[-1]
        for s in held_stocks if s in prices.columns
    )
    nav_s = pd.Series(nav_series)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")].sort_index()
    return nav_s, n_trades, cash_months


print("Running A) Monthly Rebalance...", end=" ", flush=True)
nav_a, trades_a = run_monthly_rebalance()
print("done.")

print("Running B) Hold Until Exit...  ", end=" ", flush=True)
nav_b, trades_b, cash_b = run_hold_until_exit()
print("done.")


def stats(nav_s, label, n_trades, cash_months=None):
    rm     = nav_s.pct_change().dropna()
    n_yrs  = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr   = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1/n_yrs) - 1
    sharpe = rm.mean() / rm.std() * np.sqrt(12) if rm.std() > 0 else 0
    max_dd = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()
    return {"label": label, "cagr": cagr, "sharpe": sharpe,
            "max_dd": max_dd, "final_nav": nav_s.iloc[-1],
            "n_trades": n_trades, "cash_months": cash_months,
            "nav_series": nav_s}

res_a = stats(nav_a, "A) Monthly Rebalance (V7 baseline)", trades_a)
res_b = stats(nav_b, "B) Hold Until Exit",                 trades_b, cash_b)

print("\n" + "=" * 76)
print("  DualMom — MONTHLY REBALANCE vs HOLD-UNTIL-EXIT")
print("=" * 76)
print(f"  {'Config':<38} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>12} {'Trades':>8}")
print(f"  {'-'*38} {'-'*7} {'-'*8} {'-'*8} {'-'*12} {'-'*8}")
for r in [res_a, res_b]:
    print(f"  {r['label']:<38} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} "
          f"{r['max_dd']*100:>7.1f}% Rs{r['final_nav']/1e7:>6.1f}Cr {r['n_trades']:>8,}")
print("=" * 76)

print(f"\n  Trades breakdown:")
print(f"  Monthly Rebalance : {trades_a:,} round-trip legs over 20 years (~{trades_a//20}/yr)")
print(f"  Hold Until Exit   : {trades_b:,} round-trip legs over 20 years (~{trades_b//20}/yr)")
print(f"  Trade reduction   : {(1 - trades_b/trades_a)*100:.1f}%")

# year-by-year
print("\n  Year-by-year returns:")
print(f"  {'Year':<6} {'Monthly Reb':>13} {'Hold-Until-Exit':>17} {'Diff':>7}")
print(f"  {'----':<6} {'-'*13} {'-'*17} {'-'*7}")
navs = [res_a["nav_series"].resample("YE").last(),
        res_b["nav_series"].resample("YE").last()]
all_years = sorted(set(navs[0].index.year) | set(navs[1].index.year))
for yr in all_years:
    rets = []
    for nav in navs:
        yr_idx = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yr_idx and yr_idx[0] > 0:
            rets.append((nav.iloc[yr_idx[0]] / nav.iloc[yr_idx[0]-1] - 1) * 100)
        else:
            rets.append(None)
    if all(v is not None for v in rets):
        diff = rets[0] - rets[1]
        winner = "< rebal wins" if diff > 0 else "> hold wins"
        print(f"  {yr:<6} {rets[0]:>12.1f}% {rets[1]:>16.1f}% {diff:>+6.1f}%  {winner}")

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({"Monthly_Rebalance": res_a["nav_series"],
              "Hold_Until_Exit":   res_b["nav_series"]}).to_csv(
    out_dir / "hold_until_exit_comparison.csv")
print(f"\n  NAV saved → {out_dir / 'hold_until_exit_comparison.csv'}")
