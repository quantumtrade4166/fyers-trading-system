"""
Signal history analysis — count IN/OUT transitions over 20-year backtest.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

DATA_DIR      = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS = 252
START_DATE    = "2006-01-01"
END_DATE      = "2026-06-18"

print("Loading data...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]
monthly_ends = prices.resample("ME").last().index

# build signal series
signals = []
for rebal_date in monthly_ends:
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0:
        continue
    rebal_date = prices.index[idx]
    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0:
        signals.append((rebal_date, None))
        continue
    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma      = nifty_ma100.iloc[nifty_idx]
    n_px      = nifty.iloc[nifty_idx]
    market_up = (not pd.isna(n_ma)) and (n_px > n_ma)
    signals.append((rebal_date, "IN" if market_up else "OUT"))

df = pd.DataFrame(signals, columns=["date", "signal"]).dropna()
df["year"] = pd.to_datetime(df["date"]).dt.year
df["prev"]   = df["signal"].shift(1)

in_to_out = ((df["prev"] == "IN")  & (df["signal"] == "OUT")).sum()
out_to_in = ((df["prev"] == "OUT") & (df["signal"] == "IN")).sum()

# build streaks
streaks = []
cur_sig   = df["signal"].iloc[0]
cur_start = df["date"].iloc[0]
cur_len   = 1
for i in range(1, len(df)):
    if df["signal"].iloc[i] == cur_sig:
        cur_len += 1
    else:
        streaks.append({"signal": cur_sig, "start": cur_start,
                        "end": df["date"].iloc[i-1], "months": cur_len})
        cur_sig   = df["signal"].iloc[i]
        cur_start = df["date"].iloc[i]
        cur_len   = 1
streaks.append({"signal": cur_sig, "start": cur_start,
                "end": df["date"].iloc[-1], "months": cur_len})

sdf         = pd.DataFrame(streaks)
in_streaks  = sdf[sdf["signal"] == "IN"]
out_streaks = sdf[sdf["signal"] == "OUT"]

print()
print("=" * 55)
print("  DUALMOM.LIQ.NIFTY50 — SIGNAL HISTORY (2006-2026)")
print("=" * 55)
print(f"  Total months      : {len(df)}")
print(f"  Months IN         : {(df.signal == 'IN').sum()}  ({(df.signal=='IN').mean()*100:.0f}%)")
print(f"  Months OUT        : {(df.signal == 'OUT').sum()}  ({(df.signal=='OUT').mean()*100:.0f}%)")
print()
print(f"  Total switches    : {in_to_out + out_to_in}")
print(f"    IN → OUT        : {in_to_out}  (times we sold stocks, moved to liquid fund)")
print(f"    OUT → IN        : {out_to_in}  (times we re-entered stocks)")
print()
print(f"  Distinct IN runs  : {len(in_streaks)}")
print(f"  Distinct OUT runs : {len(out_streaks)}")
print()
print(f"  IN streak  — avg {in_streaks.months.mean():.1f}m  min {in_streaks.months.min()}m  max {in_streaks.months.max()}m")
print(f"  OUT streak — avg {out_streaks.months.mean():.1f}m  min {out_streaks.months.min()}m  max {out_streaks.months.max()}m")

context = {
    2006: "Early period / data warmup",
    2007: "Bull run",
    2008: "Global Financial Crisis",
    2009: "Recovery",
    2011: "European debt crisis / RBI rate hikes",
    2015: "China slowdown scare",
    2016: "Demonetisation shock",
    2018: "IL&FS / NBFC meltdown",
    2019: "Growth slowdown",
    2020: "COVID-19 crash",
    2022: "Russia-Ukraine / Fed rate hikes",
    2025: "Global trade war fears",
    2026: "Current period",
}

print()
print("  All OUT periods (when we were in liquid fund):")
print(f"  {'Start':<11} {'End':<11} {'Months':>7}  Event")
print(f"  {'-'*11} {'-'*11} {'-'*7}  {'-'*35}")
for _, row in out_streaks.iterrows():
    yr  = pd.to_datetime(row["start"]).year
    ctx = context.get(yr, "")
    print(f"  {str(row['start'])[:10]:<11} {str(row['end'])[:10]:<11} {row['months']:>7}m  {ctx}")

print()
print("  Year-by-year signal:")
print(f"  {'Year':<6} {'IN':>4} {'OUT':>5} {'Switch':>7}")
print(f"  {'----':<6} {'--':>4} {'---':>5} {'------':>7}")
for yr, grp in df.groupby("year"):
    in_m  = (grp.signal == "IN").sum()
    out_m = (grp.signal == "OUT").sum()
    sw    = (grp.signal != grp.prev).sum()
    print(f"  {yr:<6} {in_m:>4}m {out_m:>4}m {sw:>7}")
