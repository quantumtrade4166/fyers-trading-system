"""
Debug: trace exactly what happens to NAV during bear months in the supertrend backtest
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

DATA_DIR = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
FNO_SYMBOLS = [
    "AARTIIND","ABB","ABBOTINDIA","ABCAPITAL","ABFRL","ACC","ADANIENT","ADANIPORTS",
    "ALKEM","AMBUJACEM","APOLLOHOSP","APOLLOTYRE","ASHOKLEY","ASIANPAINT","ASTRAL",
    "AXISBANK","BAJAJ-AUTO","BAJAJFINSV","BAJFINANCE","BHARTIARTL","BHEL",
    "CIPLA","COALINDIA","HCLTECH","HDFCBANK","HINDALCO","HINDUNILVR",
    "ICICIBANK","INFY","ITC","KOTAKBANK","LT","MARUTI","NTPC","ONGC",
    "RELIANCE","SBIN","SUNPHARMA","TCS","WIPRO",
]

nifty_raw = yf.download("^NSEI", start="2005-01-01", end="2026-06-18", auto_adjust=True, progress=False)
nifty = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

fno_daily  = {}
all_closes = {}
for f in DATA_DIR.glob("*.parquet"):
    sym = f.stem
    df  = pd.read_parquet(f)
    df.index = pd.to_datetime(df.index)
    all_closes[sym] = df["close"]
    if sym in FNO_SYMBOLS:
        cols = [c for c in ["open","high","low","close"] if c in df.columns]
        if len(cols) == 4:
            daily = df[cols].resample("D").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna()
            fno_daily[sym] = daily

fno_cols = list(fno_daily.keys())
prices_5min    = pd.DataFrame(all_closes).sort_index().loc["2006-01-01":"2026-06-18"]
prices_monthly = prices_5min.resample("ME").last()
fno_close_daily = pd.DataFrame({s: fno_daily[s]["close"] for s in fno_cols}).loc["2006-01-01":"2026-06-18"]
monthly_ends    = prices_monthly.index

print(f"FNO cols: {len(fno_cols)} | Monthly ends: {len(monthly_ends)}")

# Supertrend
def compute_supertrend_daily(daily_df, atr_period=10, multiplier=3.0):
    high  = daily_df["high"]
    low   = daily_df["low"]
    close = daily_df["close"]
    tr = pd.concat([(high - low), (high - close.shift(1)).abs(), (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=atr_period, adjust=False).mean()
    hl2 = (high + low) / 2
    upper_band = (hl2 + multiplier * atr).copy()
    lower_band = (hl2 - multiplier * atr).copy()
    direction  = pd.Series(True, index=daily_df.index)
    for i in range(1, len(daily_df)):
        prev_close = close.iloc[i - 1]
        prev_dir   = direction.iloc[i - 1]
        if upper_band.iloc[i] > upper_band.iloc[i-1] or prev_close > upper_band.iloc[i-1]:
            pass
        else:
            upper_band.iloc[i] = upper_band.iloc[i-1]
        if lower_band.iloc[i] < lower_band.iloc[i-1] or prev_close < lower_band.iloc[i-1]:
            pass
        else:
            lower_band.iloc[i] = lower_band.iloc[i-1]
        if prev_dir and close.iloc[i] < lower_band.iloc[i]:
            direction.iloc[i] = False
        elif not prev_dir and close.iloc[i] > upper_band.iloc[i]:
            direction.iloc[i] = True
        else:
            direction.iloc[i] = prev_dir
    return direction

print("Computing supertrend...", end=" ")
st_signals = {}
for sym, df in fno_daily.items():
    full = df.loc["2005-01-01":"2026-06-18"]
    if len(full) >= 30:
        st_signals[sym] = compute_supertrend_daily(full)
st_df = pd.DataFrame(st_signals).loc["2006-01-01":"2026-06-18"]
print(f"done {st_df.shape}")

def get_fno_close(date):
    d = pd.Timestamp(date).normalize()
    idx = fno_close_daily.index.get_indexer([d], method="ffill")[0]
    if idx < 0:
        return pd.Series(dtype=float)
    return fno_close_daily.iloc[idx]

def get_st_signal(date):
    d = pd.Timestamp(date).normalize()
    idx = st_df.index.get_indexer([d], method="ffill")[0]
    if idx < 0:
        return None
    return st_df.iloc[idx]

# --- Trace first 6 bear months ---
print("\n=== TRACING BEAR MONTHS ===")
CAPITAL   = 1_000_000.0
cash      = CAPITAL
short_cap = 0.0
short_stk = {}
alloc_per = 0.0
daily_rate = (1 + 0.06) ** (1/365) - 1
monthly_rate = (1 + 0.06) ** (1/12) - 1
prev_date  = monthly_ends[0]
bear_count = 0
LOOKBACK   = 252

for rebal_date in monthly_ends[1:]:
    # Accrue liquid on free cash
    days_el = (pd.Timestamp(rebal_date) - pd.Timestamp(prev_date)).days
    cash   *= (1 + daily_rate) ** days_el
    if short_cap > 0:
        short_cap *= (1 - 0.01 * days_el / 365)

    fno_px   = get_fno_close(rebal_date)
    short_mtm = sum(pos["shares"] * (pos["entry_px"] - fno_px.get(s, pos["entry_px"]))
                    for s, pos in short_stk.items())
    nav = cash + short_cap + short_mtm

    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_px = float(nifty.iloc[nifty_idx])
    n_ma = float(nifty_ma100.iloc[nifty_idx])
    market_up = n_px > n_ma

    lb_idx = prices_monthly.index.get_indexer([rebal_date - pd.DateOffset(months=12)], method="ffill")[0]

    if bear_count < 8 and not market_up:
        print(f"\n  {rebal_date.date()}  Nifty={n_px:.0f} MA={n_ma:.0f}  cash={cash:,.0f}  short_cap={short_cap:,.0f}  short_mtm={short_mtm:,.0f}  NAV={nav:,.0f}")
        st_now = get_st_signal(rebal_date)
        if st_now is not None:
            bearish_count = (st_now == False).sum()
            bullish_count = (st_now == True).sum()
            print(f"    Supertrend: {bearish_count} bearish (short candidates), {bullish_count} bullish")

    # Close shorts
    for sym, pos in short_stk.items():
        cp  = fno_px.get(sym, pos["entry_px"])
        if pd.isna(cp): cp = pos["entry_px"]
        pnl = pos["shares"] * (pos["entry_px"] - cp) * 0.999
        cash      += pos["alloc"] + pnl
        short_cap -= pos["alloc"]
    short_stk  = {}
    short_cap  = max(short_cap, 0.0)

    # Open new positions
    if not market_up and lb_idx >= 0:
        bear_count += 1
        st_now = get_st_signal(rebal_date)
        if st_now is not None:
            cands = [s for s in fno_cols
                     if s in st_now.index and st_now[s] == False
                     and not pd.isna(fno_px.get(s, np.nan))
                     and fno_px.get(s, 0) > 0]
            if bear_count <= 8:
                print(f"    Opening {len(cands)} shorts from {len(fno_cols)} FNO stocks")
            if cands:
                short_budget = cash * 0.30
                alloc_per    = short_budget / len(cands)
                cash        -= short_budget
                short_cap    = short_budget
                for sym in cands:
                    p = float(fno_px.get(sym, np.nan))
                    shares = alloc_per * 0.999 / p
                    short_stk[sym] = {"shares": shares, "entry_px": p, "alloc": alloc_per}

    prev_date = rebal_date
    if bear_count >= 8:
        break

print(f"\n\nTotal bear months checked: {bear_count}")
