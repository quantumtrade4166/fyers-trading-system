import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import yfinance as yf
import pandas as pd
from pathlib import Path

# Nifty 100MA status
nifty_raw = yf.download("^NSEI", period="200d", auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
ma100 = nifty.rolling(100).mean()

latest_date  = nifty.index[-1].date()
latest_price = float(nifty.iloc[-1])
latest_ma100 = float(ma100.iloc[-1])
gap_pct      = (latest_price - latest_ma100) / latest_ma100 * 100
in_market    = latest_price > latest_ma100

print("=" * 45)
print("  DUAL MOMENTUM — CURRENT STATUS")
print("=" * 45)
print(f"  As of       : {latest_date}")
print(f"  Nifty close : {latest_price:,.2f}")
print(f"  100-day MA  : {latest_ma100:,.2f}")
print(f"  Gap         : {gap_pct:+.2f}%")
print(f"  Signal      : {'IN  -- Nifty ABOVE 100MA' if in_market else 'OUT -- Nifty BELOW 100MA'}")
print("=" * 45)

if in_market:
    # show current top 50 momentum stocks
    DATA_DIR = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
    frames = {}
    for f in DATA_DIR.glob("*.parquet"):
        df = pd.read_parquet(f, columns=["close"])
        df.index = pd.to_datetime(df.index)
        frames[f.stem] = df["close"]

    prices = pd.DataFrame(frames).sort_index()

    # 12-month return as of today
    today_idx    = len(prices) - 1
    lookback_idx = today_idx - 252
    if lookback_idx >= 0:
        current_px   = prices.iloc[today_idx]
        past_px      = prices.iloc[lookback_idx]
        returns_12m  = (current_px / past_px - 1).dropna().sort_values(ascending=False)
        top50        = returns_12m.head(50)

        print(f"\n  Top 50 momentum stocks (12m return as of {prices.index[today_idx].date()}):")
        print(f"  {'Rank':<5} {'Symbol':<20} {'12m Return':>10}")
        print(f"  {'-'*5} {'-'*20} {'-'*10}")
        for rank, (sym, ret) in enumerate(top50.items(), 1):
            print(f"  {rank:<5} {sym:<20} {ret*100:>9.1f}%")
else:
    print("\n  Currently in CASH / Liquid Fund.")
    print("  Re-entry trigger: Nifty closes above 100-day MA at any month-end.")
