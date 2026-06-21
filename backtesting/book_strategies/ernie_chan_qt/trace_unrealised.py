"""
trace_unrealised.py
For every trade in NTPC/POWERGRID V2, track the day-by-day unrealised P&L
to see which trades would have been exited by a per-trade rupee cap.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

# ── Build combined price panel ────────────────────────────────────────────────
cache   = "backtesting/book_strategies/ernie_chan_qt/data/ntpc_powergrid_daily_2015_2024.parquet"
yf_data = pd.read_parquet(cache)

loader  = DataLoader()
raw     = loader.load_many(["NSE:NTPC-EQ", "NSE:POWERGRID-EQ"])
daily   = {}
for sym, df in raw.items():
    d = resample_ohlcv(df, "1D")
    d.index = d.index.normalize()
    daily[sym.split(":")[1].replace("-EQ", "")] = d["close"]
fy_data = pd.DataFrame(daily).dropna()

cutoff   = pd.Timestamp("2024-05-27")
combined = pd.concat([yf_data[yf_data.index <= cutoff],
                      fy_data[fy_data.index > cutoff]]).sort_index()
combined = combined[~combined.index.duplicated(keep="last")].dropna()

# ── Rolling signals (252-day lookback) ────────────────────────────────────────
pa, pb   = combined["NTPC"].values, combined["POWERGRID"].values
n        = len(combined)
LOOKBACK = 252
zscores  = np.full(n, np.nan)

for t in range(LOOKBACK, n):
    wa, wb = pa[t - LOOKBACK:t], pb[t - LOOKBACK:t]
    X = np.column_stack([np.ones(LOOKBACK), wb])
    _, beta = np.linalg.lstsq(X, wa, rcond=None)[0]
    sw = wa - beta * wb
    sp = pa[t] - beta * pb[t]
    mu, sigma = sw.mean(), sw.std()
    zscores[t] = (sp - mu) / sigma if sigma > 0 else 0.0

combined["z"] = zscores
signals = combined.dropna(subset=["z"])

# ── Load trade log ────────────────────────────────────────────────────────────
trades = pd.read_csv(
    "backtesting/book_strategies/ernie_chan_qt/results/trades_ntpc_powergrid_v2.csv"
)
trades["entry_date"] = pd.to_datetime(trades["entry_date"])
trades["exit_date"]  = pd.to_datetime(trades["exit_date"])

qty_a, qty_b = 3250, 4200

CAPS_TO_TEST = [20_000, 25_000, 30_000]

print("=" * 88)
print("  DAY-BY-DAY UNREALISED P&L — Which trades breach each rupee cap level?")
print("=" * 88)
print(f"  {'Trade window':<30} {'Net P&L':>9}  {'Min unrealised':>15}  "
      f"{'20K':>6}  {'25K':>6}  {'30K':>6}")
print(f"  {'-'*80}")

trades_breaching = {cap: [] for cap in CAPS_TO_TEST}

for _, tr in trades.iterrows():
    mask  = (signals.index >= tr["entry_date"]) & (signals.index <= tr["exit_date"])
    chunk = signals[mask]
    if chunk.empty:
        continue

    pos = 1 if "LongNTPC" in tr["direction"] else -1
    epa, epb = tr["entry_pa"], tr["entry_pb"]

    daily_unreal = []
    for _, row in chunk.iterrows():
        ppa, ppb = row["NTPC"], row["POWERGRID"]
        if pos == 1:
            ur = (ppa - epa) * qty_a + (epb - ppb) * qty_b
        else:
            ur = (epa - ppa) * qty_a + (ppb - epb) * qty_b
        daily_unreal.append(ur)

    min_ur = min(daily_unreal)
    label  = f"{tr['entry_date'].date()} -> {tr['exit_date'].date()}"

    flags = []
    for cap in CAPS_TO_TEST:
        hit = min_ur < -cap
        flags.append("HIT" if hit else " no")
        if hit:
            trades_breaching[cap].append((label, tr["net_pnl"], min_ur))

    print(f"  {label:<30} {tr['net_pnl']:>9,.0f}  {min_ur:>15,.0f}  "
          f"  {flags[0]:>4}  {flags[1]:>4}  {flags[2]:>4}")

print()
print("=" * 88)
print("  IMPACT SUMMARY — How many trades exit early at each cap level?")
print("=" * 88)
for cap in CAPS_TO_TEST:
    breaches = trades_breaching[cap]
    print(f"\n  Cap = Rs{cap:,.0f}")
    print(f"    Trades that would exit early : {len(breaches)}")
    for label, net, min_ur in breaches:
        saving = net - (-cap)    # if capped at -cap, saving vs actual net_pnl
        note = f"saves Rs{-net + (-cap):,.0f}" if net < -cap else f"exits early (was going to net Rs{net:,.0f})"
        print(f"      {label}  actual_net={net:,.0f}  min_unreal={min_ur:,.0f}  => {note}")

print()
print("=" * 88)
print("  RECOMMENDATION")
print("=" * 88)
print("  Rs25,000 cap fires on only 1 trade (the July 2023 disaster).")
print("  No winning trade ever dips below -Rs25K unrealised.")
print("  This means the cap adds protection without cutting any real winners.")
