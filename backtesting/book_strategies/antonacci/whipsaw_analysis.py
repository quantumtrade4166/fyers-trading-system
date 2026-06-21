"""
Whipsaw analysis — find months where we entered and exited the very next month
Momentum Weighted, TOP_N=50, Nifty 100MA filter, no stop loss
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

print("Downloading Nifty 50 (^NSEI)...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]

prices = pd.DataFrame(frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]
monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")

# ── Run backtest tracking month-by-month status ────────────────────────────────
rebal_log = []   # one entry per month-end
cash_value    = CAPITAL
held_stocks   = {}
prev_date     = monthly_ends[0]

for i, rebal_date in enumerate(monthly_ends[1:], 1):
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0:
        continue
    rebal_date     = prices.index[idx]
    current_px     = prices.iloc[idx]
    months_elapsed = (rebal_date - prev_date).days / 30.44
    cash_value    *= (1 + monthly_rate) ** months_elapsed

    nav = cash_value
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            nav += shares * p

    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0:
        prev_date = rebal_date
        continue

    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma      = nifty_ma100.iloc[nifty_idx]
    n_px      = nifty.iloc[nifty_idx]
    market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

    past_px     = prices.iloc[lb_idx]
    returns_12m = (current_px / past_px - 1).dropna()
    candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

    # sell everything
    sell_value = cash_value
    for sym, shares in held_stocks.items():
        p = current_px.get(sym, np.nan)
        if not pd.isna(p):
            sell_value += shares * p * (1 - SLIPPAGE_PCT)

    held_stocks = {}
    cash_value  = sell_value

    # record BEFORE buying — this nav is end of this month
    prev_entry = rebal_log[-1] if rebal_log else None
    prev_nav   = prev_entry["nav_end"] if prev_entry else CAPITAL
    month_ret  = (nav / prev_nav - 1) * 100 if prev_nav > 0 else 0

    rebal_log.append({
        "date":       rebal_date,
        "status":     "IN" if market_up else "OUT",
        "nav_end":    nav,
        "month_ret":  month_ret,
        "nifty":      float(n_px),
        "ma100":      float(n_ma) if not pd.isna(n_ma) else 0,
        "candidates": candidates[:5],  # top 5 for reference
    })

    if candidates:
        raw    = {s: max(returns_12m[s], 0.001) for s in candidates}
        total  = sum(raw.values())
        weights = {s: v/total for s, v in raw.items()}
        invested = 0
        for sym, w in weights.items():
            p = current_px.get(sym, np.nan)
            if pd.isna(p) or p <= 0:
                continue
            cost = sell_value * w * (1 + SLIPPAGE_PCT)
            held_stocks[sym] = cost / p
            invested += cost
        cash_value = max(sell_value - invested, 0)

    prev_date = rebal_date

# ── Find whipsaw months ────────────────────────────────────────────────────────
# Whipsaw = entered (IN) at month M, then OUT at month M+1
whipsaws = []
for i in range(1, len(rebal_log)):
    prev = rebal_log[i - 1]
    curr = rebal_log[i]
    if prev["status"] == "IN" and curr["status"] == "OUT":
        # we were invested during this month and got stopped by 100MA at end
        whipsaws.append({
            "entry_month":  prev["date"].strftime("%b %Y"),
            "exit_month":   curr["date"].strftime("%b %Y"),
            "month_ret":    curr["month_ret"],   # return during the invested month
            "nav_entry":    prev["nav_end"],
            "nav_exit":     curr["nav_end"],
            "nifty_entry":  prev["nifty"],
            "nifty_exit":   curr["nifty"],
            "top5":         prev["candidates"],
        })

print("=" * 75)
print("  WHIPSAW ANALYSIS — Entered IN, Exited OUT next month")
print("  (Momentum Weighted | Nifty 100MA | TOP_N=50)")
print("=" * 75)
print(f"  Total whipsaws: {len(whipsaws)} over {len(rebal_log)} months")
print()
print(f"  {'#':<4} {'Entry':>9} {'Exit':>9} {'Month Ret%':>11} {'NAV Entry':>13} {'NAV Exit':>13} {'Nifty Drop%':>12}")
print(f"  {'-'*4} {'-'*9} {'-'*9} {'-'*11} {'-'*13} {'-'*13} {'-'*12}")

total_ret = 0
for j, w in enumerate(whipsaws, 1):
    nifty_chg = (w["nifty_exit"] / w["nifty_entry"] - 1) * 100
    print(f"  {j:<4} {w['entry_month']:>9} {w['exit_month']:>9} {w['month_ret']:>10.2f}% "
          f"{w['nav_entry']:>13,.0f} {w['nav_exit']:>13,.0f} {nifty_chg:>11.2f}%")
    total_ret += w["month_ret"]

print(f"\n  Avg return in whipsaw months : {total_ret/len(whipsaws):.2f}%")
print(f"  Positive whipsaws (lucky)    : {sum(1 for w in whipsaws if w['month_ret'] > 0)}")
print(f"  Negative whipsaws (hurt)     : {sum(1 for w in whipsaws if w['month_ret'] < 0)}")
print(f"  Worst whipsaw                : {min(whipsaws, key=lambda x: x['month_ret'])['month_ret']:.2f}% "
      f"({min(whipsaws, key=lambda x: x['month_ret'])['exit_month']})")
print(f"  Best whipsaw                 : {max(whipsaws, key=lambda x: x['month_ret'])['month_ret']:.2f}% "
      f"({max(whipsaws, key=lambda x: x['month_ret'])['exit_month']})")

# ── Also show all IN/OUT transitions ──────────────────────────────────────────
print(f"\n{'='*50}")
print("  FULL SIGNAL HISTORY (transitions only)")
print(f"{'='*50}")
print(f"  {'Date':>10} {'Signal':>7} {'Month Ret%':>11} {'Nifty':>8} {'vs 100MA':>9}")
print(f"  {'-'*10} {'-'*7} {'-'*11} {'-'*8} {'-'*9}")
prev_status = None
for entry in rebal_log:
    if entry["status"] != prev_status:
        gap = ((entry["nifty"] / entry["ma100"]) - 1) * 100 if entry["ma100"] > 0 else 0
        flag = " << WHIPSAW" if (prev_status == "IN" and entry["status"] == "OUT") else ""
        print(f"  {entry['date'].strftime('%b %Y'):>10} {entry['status']:>7} {entry['month_ret']:>10.2f}% "
              f"{entry['nifty']:>8,.0f} {gap:>+8.2f}%{flag}")
        prev_status = entry["status"]
