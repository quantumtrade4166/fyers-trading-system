"""
Dual Momentum V8 — Portfolio Stop Loss comparison
Momentum Weighted, TOP_N=50, Nifty 100MA, 6% liquid fund

A) No stop loss
B) 7% monthly portfolio stop — if NAV drops 7% from month-start NAV, exit to cash that day
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
STOP_PCT       = 0.07
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
monthly_ends = prices.resample("ME").last().index
daily_rate   = (1 + LIQUID_FUND_PA) ** (1/365) - 1
monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Price matrix: {prices.shape[0]} days x {prices.shape[1]} symbols\n")


def run_backtest(use_stop, label):
    cash_value      = CAPITAL
    held_stocks     = {}          # sym -> shares
    month_start_nav = CAPITAL
    stopped_out     = False
    stop_hits       = 0
    nav_monthly     = {monthly_ends[0]: CAPITAL}
    prev_rebal_idx  = prices.index.get_indexer([monthly_ends[0]], method="ffill")[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        cur_idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if cur_idx < 0:
            continue
        rebal_date = prices.index[cur_idx]

        # ── Daily stop loss scan between prev rebal and this rebal ────────────
        if use_stop and held_stocks and not stopped_out:
            for d_idx in range(prev_rebal_idx + 1, cur_idx + 1):
                d      = prices.index[d_idx]
                day_px = prices.iloc[d_idx]

                # skip day if too many NaN prices (non-trading day / data gap)
                valid_prices = {sym: day_px.get(sym, np.nan) for sym in held_stocks
                                if not pd.isna(day_px.get(sym, np.nan))}
                if len(valid_prices) < len(held_stocks) * 0.5:
                    continue  # skip days where >50% stocks have no data

                days_since  = (d - prices.index[prev_rebal_idx]).days
                cash_now    = cash_value * (1 + daily_rate) ** days_since
                stock_val   = sum(shares * valid_prices[sym]
                                  for sym, shares in held_stocks.items()
                                  if sym in valid_prices)
                daily_nav = cash_now + stock_val

                if month_start_nav > 0 and (daily_nav / month_start_nav - 1) <= -STOP_PCT:
                    sell_val = daily_nav * (1 - SLIPPAGE_PCT)
                    remaining_days = (rebal_date - d).days
                    cash_value  = sell_val * (1 + daily_rate) ** remaining_days
                    held_stocks = {}
                    stopped_out = True
                    stop_hits  += 1
                    break

        # ── Accrue liquid fund on cash from prev rebal to this rebal ─────────
        # If stop fired mid-month, cash was already accrued up to stop day
        # and then for remaining days after stop. Don't accrue again.
        if not (use_stop and stopped_out):
            days_in_month = (rebal_date - prices.index[prev_rebal_idx]).days
            cash_value   *= (1 + daily_rate) ** days_in_month

        # ── Mark to market at month-end ───────────────────────────────────────
        current_px = prices.iloc[cur_idx]
        stock_nav  = sum(
            shares * current_px.get(sym, np.nan)
            for sym, shares in held_stocks.items()
            if not pd.isna(current_px.get(sym, np.nan))
        )
        nav = cash_value + stock_nav

        # ── Absolute filter ───────────────────────────────────────────────────
        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        lb_idx = cur_idx - LOOKBACK_DAYS
        past_px     = prices.iloc[lb_idx] if lb_idx >= 0 else None
        returns_12m = ((current_px / past_px) - 1).dropna() if past_px is not None else pd.Series(dtype=float)

        # candidates — skip if stopped out this month OR market is down
        enter = market_up and not stopped_out and lb_idx >= 0
        candidates = returns_12m.nlargest(TOP_N).index.tolist() if enter else []

        # ── Sell all current positions (skip if stop already liquidated them) ──
        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)

        held_stocks = {}
        cash_value  = sell_value  # held_stocks is already empty if stop fired

        # ── Buy new candidates ────────────────────────────────────────────────
        if candidates:
            raw     = {s: max(returns_12m[s], 0.001) for s in candidates}
            total   = sum(raw.values())
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

        nav_monthly[rebal_date] = nav
        month_start_nav = nav
        stopped_out     = False
        prev_rebal_idx  = cur_idx

    # final NAV
    final_nav = cash_value + sum(
        shares * prices[sym].dropna().iloc[-1]
        for sym, shares in held_stocks.items()
        if sym in prices.columns
    )
    nav_monthly[prices.index[-1]] = final_nav

    nav_s     = pd.Series(nav_monthly).sort_index()
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {"label": label, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
            "final_nav": final_nav, "stop_hits": stop_hits, "nav_series": nav_s}


configs = [
    (False, "A) No Stop Loss"),
    (True,  "B) 7% Monthly Stop Loss"),
]

results = []
for use_stop, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(use_stop, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.2f}%  StopHits={r['stop_hits']}")


def yearly_stats(nav_s):
    rows = []
    for yr in sorted(set(nav_s.index.year)):
        yr_data = nav_s[nav_s.index.year == yr]
        if len(yr_data) < 2:
            continue
        prev      = nav_s[nav_s.index.year == yr - 1]
        start_nav = float(prev.iloc[-1]) if len(prev) > 0 else float(yr_data.iloc[0])
        end_nav   = float(yr_data.iloc[-1])
        if start_nav == 0:
            continue
        pnl     = end_nav - start_nav
        ret_pct = (end_nav / start_nav - 1) * 100
        peak    = yr_data.cummax()
        dd      = float(((yr_data - peak) / peak * 100).min())
        rows.append({"Year": yr, "Start": start_nav, "End": end_nav,
                     "PnL": pnl, "Ret%": ret_pct, "MaxDD%": dd})
    return rows


rows_a = yearly_stats(results[0]["nav_series"])
rows_b = yearly_stats(results[1]["nav_series"])

for r, rows in zip(results, [rows_a, rows_b]):
    print(f"\n{'='*80}")
    print(f"  {r['label']}  |  CAGR: {r['cagr']*100:.2f}%  |  Sharpe: {r['sharpe']:.3f}  |  MaxDD: {r['max_dd']*100:.2f}%  |  Final: Rs {r['final_nav']:,.0f}")
    print(f"{'='*80}")
    print(f"  {'Year':<6} {'Start NAV':>13} {'End NAV':>13} {'P&L (Rs)':>13} {'Return%':>9} {'MaxDD%':>8}")
    print(f"  {'-'*6} {'-'*13} {'-'*13} {'-'*13} {'-'*9} {'-'*8}")
    for row in rows:
        flag = " *" if row["Ret%"] < 0 else ""
        print(f"  {row['Year']:<6} {row['Start']:>13,.0f} {row['End']:>13,.0f} {row['PnL']:>13,.0f} {row['Ret%']:>8.1f}%{flag} {row['MaxDD%']:>7.1f}%")

print(f"\n{'='*86}")
print("  SIDE-BY-SIDE — NO STOP vs 7% STOP")
print(f"{'='*86}")
print(f"  {'Year':<6} {'NoStop Ret%':>12} {'NoStop DD%':>11} {'NoStop PnL':>13} | {'Stop Ret%':>10} {'Stop DD%':>9} {'Stop PnL':>13} {'Winner':>7}")
print(f"  {'-'*6} {'-'*12} {'-'*11} {'-'*13}   {'-'*10} {'-'*9} {'-'*13} {'-'*7}")
for a, b in zip(rows_a, rows_b):
    winner = "STOP" if abs(b["MaxDD%"]) < abs(a["MaxDD%"]) else "NOSTOP"
    print(f"  {a['Year']:<6} {a['Ret%']:>11.1f}% {a['MaxDD%']:>10.1f}% {a['PnL']:>13,.0f} | {b['Ret%']:>9.1f}% {b['MaxDD%']:>8.1f}% {b['PnL']:>13,.0f} {winner:>7}")

print(f"\n  Stop triggered  : {results[1]['stop_hits']} times over 20 years")
print(f"  CAGR cost       : {(results[0]['cagr'] - results[1]['cagr'])*100:.2f}%")
print(f"  DD improvement  : {(abs(results[1]['max_dd']) - abs(results[0]['max_dd']))*100:.2f}% reduction")
