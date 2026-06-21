import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
import pandas as pd
import numpy as np

trades = pd.read_csv(
    "backtesting/book_strategies/ernie_chan_qt/results/trades_ntpc_powergrid_v2.csv"
)
trades["entry_date"] = pd.to_datetime(trades["entry_date"])
trades["exit_date"]  = pd.to_datetime(trades["exit_date"])

ntpc_avg  = 160.0
pg_avg    = 130.0
qty_a, qty_b = 3250, 4200
notional_a = qty_a * ntpc_avg
notional_b = qty_b * pg_avg
total_notional = notional_a + notional_b

# ── GAP 1: Missing transaction costs ────────────────────────────────────────
print("=== GAP 1: MISSING TRANSACTION COSTS ===")
brokerage    = 0.0003
stt_sell     = 0.0001
exchange_levy = 0.0000195
gst_rate      = 0.18
stamp_duty    = 0.00003

modeled = (notional_a * brokerage * 2 + notional_a * stt_sell +
           notional_b * brokerage * 2 + notional_b * stt_sell)

gst_part   = (notional_a + notional_b) * brokerage * 2 * gst_rate
exch_part  = total_notional * exchange_levy * 2
stamp_part = total_notional * stamp_duty
actual     = modeled + gst_part + exch_part + stamp_part

print(f"  Modeled per trade (brokerage + STT only)   : Rs{modeled:,.0f}")
print(f"  Actual  per trade (+GST +exchange +stamp)  : Rs{actual:,.0f}")
print(f"  Underestimate per trade                    : Rs{actual-modeled:,.0f}  ({(actual/modeled-1)*100:.0f}% more)")
print(f"  Over 30 trades                             : Rs{(actual-modeled)*30:,.0f} missing")
print()

# ── GAP 2: Futures roll cost ─────────────────────────────────────────────────
print("=== GAP 2: FUTURES ROLL COST (not modeled at all) ===")
avg_hold         = 31.3
roll_pct         = 0.60
roll_cost_pct    = 0.0002    # 0.02% of notional per roll (realistic bid-ask)
roll_cost_each   = total_notional * roll_cost_pct
rolls_total      = 30 * roll_pct
total_roll_cost  = roll_cost_each * rolls_total
print(f"  Avg hold period                   : {avg_hold:.0f} days")
print(f"  Trades crossing month-end (~60%)  : {rolls_total:.0f} out of 30")
print(f"  Roll cost per event (0.02% notl.) : Rs{roll_cost_each:,.0f}")
print(f"  Total roll cost estimate          : Rs{total_roll_cost:,.0f}")
print(f"  As % of net P&L (Rs3.23L)        : {total_roll_cost/323439*100:.1f}%")
print()

# ── GAP 3: Entry timing ─────────────────────────────────────────────────────
print("=== GAP 3: ENTRY TIMING (look-ahead risk) ===")
print("  Signal: z-score computed at close of day T")
print("  Entry : we assume fill at close of day T")
print("  Problem: you see the close AFTER 15:30 -- too late to enter SAME day")
print("  Fix options:")
print("    A) MOC (Market on Close) orders -- submit before 15:28, filled at close")
print("       Impact: zero, but needs broker support")
print("    B) Enter next-day open -- tiny price difference, no look-ahead")
print("  Our backtest uses approach A implicitly -- fine but worth noting.")
print()

# ── GAP 4: Lookback optimized on full data ───────────────────────────────────
print("=== GAP 4: LOOKBACK CHOSEN WITH HINDSIGHT ===")
print("  We set LOOKBACK=252 because half-life = 124d (measured on all 11 years)")
print("  A trader starting in 2018 would have seen a different half-life")
print("  Impact: mild optimisation bias -- LOOKBACK of 252 is a reasonable")
print("  heuristic anyway (1 trading year); real-world choice would be similar")
print()

# ── GAP 5: 2023 breakdown ───────────────────────────────────────────────────
print("=== GAP 5: 2023 -- THE ONLY LOSING YEAR ===")
t2023 = trades[trades["exit_date"].dt.year == 2023].sort_values("entry_date")
cols = ["entry_date","exit_date","hold_days","direction","exit_reason","net_pnl"]
print(t2023[cols].to_string(index=False))
pnl_2023 = t2023["net_pnl"].sum()
print(f"\n  Total 2023 P&L : Rs{pnl_2023:,.0f}")
losers = t2023[t2023["net_pnl"] < 0]
print(f"  Losing trades  : {len(losers)}")
print(f"  Avg loss size  : Rs{losers['net_pnl'].mean():,.0f}")
print()
print("  Root cause: no ROLLING cointegration check.")
print("  We ran ONE global ADF test on 11-year data and assumed it always holds.")
print("  If the pair temporarily stopped being cointegrated in 2023")
print("  (govt transmission policy shift), we had no mechanism to detect and pause.")
print()

# ── GAP 6: No per-trade rupee cap ───────────────────────────────────────────
print("=== GAP 6: NO PER-TRADE RUPEE LOSS CAP ===")
print("  Current stop: z-score based (exit if |z| > 3.5 sigma)")
print("  Statistical stops can still result in large Rs losses if sigma is large")
max_loss = trades["net_pnl"].min()
print(f"  Largest single trade loss in backtest: Rs{max_loss:,.0f}")
print("  Improvement: add a hard cap e.g. 'exit if this trade loses > Rs25K'")
print("  This prevents a single bad trade from dominating the annual P&L")
print()

# ── Summary ──────────────────────────────────────────────────────────────────
print("=" * 58)
print("  REALISTIC P&L AFTER ACCOUNTING FOR GAPS")
print("=" * 58)
reported_pnl = 323439
missing_txn  = (actual - modeled) * 30
missing_roll = total_roll_cost
adj_pnl      = reported_pnl - missing_txn - missing_roll
print(f"  Backtest net P&L (11yr)         : Rs{reported_pnl:,.0f}")
print(f"  Missing txn costs (30 trades)   : Rs{missing_txn:,.0f}")
print(f"  Missing roll costs (~18 rolls)  : Rs{missing_roll:,.0f}")
print(f"  Realistic adjusted P&L estimate : Rs{adj_pnl:,.0f}")
print(f"  Haircut                         : {(1-adj_pnl/reported_pnl)*100:.1f}%")
print(f"  Still profitable: {'YES' if adj_pnl > 0 else 'NO'}")
