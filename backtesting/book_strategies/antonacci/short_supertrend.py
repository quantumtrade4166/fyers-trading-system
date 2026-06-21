"""
Short-side standalone backtest — Supertrend on daily timeframe

Only active during bear periods (Nifty < 100MA at month-end).
30% of portfolio allocated to short book, 70% in liquid fund.

Entry  : Stock below supertrend at month-end rebalance
Exit   : Stock closes above supertrend on any day intra-month (covered next open)
         OR month-end when Nifty > 100MA (regime flips to long)

Compares:
  A) Liquid Fund only (no short) — benchmark
  B) Short: below supertrend only (Option 3)
  C) Short: below supertrend AND 12m return negative (Option 2+3)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

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
    "ATUL","AUBANK","AUROPHARMA","AXISBANK","BAJAJ-AUTO","BAJAJFINSV","BAJFINANCE",
    "BALKRISIND","BANDHANBNK","BANKBARODA","BATAINDIA","BEL","BERGEPAINT","BHARATFORG",
    "BHARTIARTL","BHEL","BIOCON","BOSCHLTD","BPCL","BRITANNIA","BSOFT","CANBK",
    "CANFINHOME","CHAMBLFERT","CHOLAFIN","CIPLA","COALINDIA","COFORGE","COLPAL",
    "CONCOR","COROMANDEL","CROMPTON","CUB","CUMMINSIND","DABUR","DALBHARAT",
    "DEEPAKNTR","DELTACORP","DIVISLAB","DIXON","DLF","DRREDDY","EICHERMOT","ESCORTS",
    "EXIDEIND","FEDERALBNK","GAIL","GLENMARK","GNFC","GODREJCP","GODREJPROP",
    "GRANULES","GRASIM","GUJGASLTD","HAL","HAVELLS","HCLTECH","HDFCAMC","HDFCBANK",
    "HDFCLIFE","HEROMOTOCO","HINDALCO","HINDCOPPER","HINDPETRO","HINDUNILVR",
    "ICICIBANK","ICICIGI","ICICIPRULI","IDEA","IDFCFIRSTB","IEX","IGL","INDHOTEL",
    "INDIAMART","INDUSINDBK","INDUSTOWER","INFY","INTELLECT","IOC","IPCALAB","IRCTC",
    "ITC","JINDALSTEL","JKCEMENT","JSL","JSWSTEEL","JUBLFOOD","KOTAKBANK","LALPATHLAB",
    "LAURUSLABS","LICHSGFIN","LT","LTIM","LTTS","LUPIN","M&M","M&MFIN","MANAPPURAM",
    "MARICO","MARUTI","MCX","METROPOLIS","MFSL","MGL","MOTHERSON","MPHASIS","MRF",
    "MUTHOOTFIN","NATIONALUM","NAUKRI","NAVINFLUOR","NESTLEIND","NMDC","NTPC",
    "OBEROIRLTY","OFSS","ONGC","PAGEIND","PERSISTENT","PETRONET","PFC","PIDILITIND",
    "PIIND","PNB","POLYCAB","POWERGRID","PVRINOX","RAMCOCEM","RBLBANK","RECLTD",
    "RELIANCE","SAIL","SBICARD","SBILIFE","SBIN","SHREECEM","SHRIRAMFIN","SIEMENS",
    "SRF","SUNPHARMA","SUNTV","SUPREMEIND","SUZLON","SYNGENE","TATACHEM","TATACOMM",
    "TATACONSUM","TATAMOTORS","TATAPOWER","TATASTEEL","TCS","TECHM","TIINDIA","TITAN",
    "TORNTPHARM","TORNTPOWER","TRENT","TVSMOTOR","UBL","ULTRACEMCO","UPL","VEDL",
    "VOLTAS","WIPRO","ZYDUSLIFE",
]

LOOKBACK_DAYS  = 252
TOP_N_LONG     = 50
SHORT_ALLOC    = 0.30
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
SHORT_COST_PA  = 0.01
ST_ATR_PERIOD  = 10
ST_MULTIPLIER  = 3.0
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"

monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
daily_rate   = (1 + LIQUID_FUND_PA) ** (1/365) - 1


# ── Supertrend (corrected band logic) ─────────────────────────────────────────
def compute_supertrend(high, low, close, atr_period=10, multiplier=3.0):
    """
    Returns boolean Series: True = bullish, False = bearish (short signal)
    Uses standard supertrend band clamping logic.
    """
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(span=atr_period, adjust=False).mean()

    hl2        = (high + low) / 2
    basic_up   = hl2 + multiplier * atr
    basic_dn   = hl2 - multiplier * atr

    final_up   = basic_up.copy()
    final_dn   = basic_dn.copy()
    direction  = pd.Series(True, index=close.index)   # True = bullish

    for i in range(1, len(close)):
        # upper band: can only decrease (tighten from above)
        if basic_up.iloc[i] < final_up.iloc[i-1] or close.iloc[i-1] > final_up.iloc[i-1]:
            final_up.iloc[i] = basic_up.iloc[i]
        else:
            final_up.iloc[i] = final_up.iloc[i-1]

        # lower band: can only increase (tighten from below)
        if basic_dn.iloc[i] > final_dn.iloc[i-1] or close.iloc[i-1] < final_dn.iloc[i-1]:
            final_dn.iloc[i] = basic_dn.iloc[i]
        else:
            final_dn.iloc[i] = final_dn.iloc[i-1]

        # direction
        prev_dir = direction.iloc[i-1]
        if prev_dir:                                   # was bullish
            direction.iloc[i] = close.iloc[i] >= final_dn.iloc[i]
        else:                                          # was bearish
            direction.iloc[i] = close.iloc[i] > final_up.iloc[i]

    return direction   # True = bullish, False = bearish


# ── Load data ──────────────────────────────────────────────────────────────────
print("Downloading Nifty 50 (^NSEI)...")
nifty_raw   = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty_close = nifty_raw["Close"].squeeze()
nifty_close.index = pd.to_datetime(nifty_close.index).tz_localize(None).normalize()
nifty_ma100 = nifty_close.rolling(100).mean()

print("Loading 500 symbols (5-min) and resampling FNO to daily...")
raw_frames = {}
fno_daily  = {}   # sym -> daily OHLC DataFrame

for f in DATA_DIR.glob("*.parquet"):
    sym = f.stem
    df  = pd.read_parquet(f)
    df.index = pd.to_datetime(df.index)
    raw_frames[sym] = df["close"]

    if sym in FNO_SYMBOLS:
        cols = [c for c in ["open","high","low","close"] if c in df.columns]
        if len(cols) == 4:
            daily = df[cols].resample("D").agg({
                "open": "first", "high": "max", "low": "min", "close": "last"
            }).dropna(how="all")
            daily.index = daily.index.normalize()
            fno_daily[sym] = daily

# 5-min close matrix for monthly snapshots (same as working backtests)
prices = pd.DataFrame(raw_frames).sort_index()
prices = prices.loc[START_DATE:END_DATE]

fno_cols = [s for s in FNO_SYMBOLS if s in fno_daily]

# daily close matrix for FNO (for intra-month P&L tracking)
fno_close_d = pd.DataFrame({
    s: fno_daily[s]["close"] for s in fno_cols
}).sort_index()
fno_close_d.index = fno_close_d.index.normalize()
fno_close_d = fno_close_d.loc[START_DATE:END_DATE]

monthly_ends = prices.resample("ME").last().index
print(f"  5-min matrix  : {prices.shape}")
print(f"  FNO daily     : {fno_close_d.shape}  ({len(fno_cols)} symbols)")
print(f"  Monthly ends  : {len(monthly_ends)} months\n")


# ── Compute daily supertrend for all FNO stocks ────────────────────────────────
print("Computing daily supertrend for all FNO stocks...", end=" ", flush=True)
st_bull = {}   # sym -> daily boolean Series (True=bullish)
for sym in fno_cols:
    df = fno_daily[sym].loc["2005-01-01":END_DATE]
    if len(df) < ST_ATR_PERIOD + 20:
        continue
    st_bull[sym] = compute_supertrend(
        df["high"], df["low"], df["close"], ST_ATR_PERIOD, ST_MULTIPLIER
    )

# align to common daily index
st_df = pd.DataFrame(st_bull).sort_index()
st_df.index = st_df.index.normalize()
st_df = st_df.loc[START_DATE:END_DATE]
print(f"done. {st_df.shape}  (True=bullish, False=bearish/short)")


def st_at(date):
    """Get supertrend signals on or before date. Returns Series[bool]."""
    d   = pd.Timestamp(date).normalize()
    idx = st_df.index.get_indexer([d], method="ffill")[0]
    return st_df.iloc[idx] if idx >= 0 else None

def fno_px_at(date):
    """Get FNO daily close on or before date."""
    d   = pd.Timestamp(date).normalize()
    idx = fno_close_d.index.get_indexer([d], method="ffill")[0]
    return fno_close_d.iloc[idx] if idx >= 0 else pd.Series(dtype=float)


# ── Backtest ───────────────────────────────────────────────────────────────────
def run_backtest(use_short, require_neg_12m, momentum_weighted, label):
    nav_series    = {}
    cash_value    = float(CAPITAL)
    long_stocks   = {}    # sym -> shares
    short_stocks  = {}    # sym -> {"shares", "entry_px", "alloc"}
    short_capital = 0.0
    prev_date     = monthly_ends[0]
    prev_idx      = prices.index.get_indexer([monthly_ends[0]], method="ffill")[0]
    total_covers  = 0
    bear_months   = 0

    nav_series[prices.index[prev_idx]] = cash_value

    for rebal_date in monthly_ends[1:]:
        # snap to actual last trading bar of month (same as working scripts)
        cur_idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if cur_idx < 0:
            continue
        rebal_date = prices.index[cur_idx]       # actual timestamp in prices
        rebal_day  = pd.Timestamp(rebal_date).normalize()   # date for daily lookups

        # ── Intra-month daily exit: cover shorts above supertrend ──────────
        if use_short and short_stocks:
            prev_day = pd.Timestamp(prev_date).normalize()
            daily_mask = (fno_close_d.index > prev_day) & (fno_close_d.index < rebal_day)
            daily_dates = fno_close_d.index[daily_mask]

            for d in daily_dates:
                if not short_stocks:
                    break
                st_row = st_df.loc[d] if d in st_df.index else None
                if st_row is None:
                    continue
                day_px = fno_close_d.loc[d]

                to_cover = [s for s in list(short_stocks.keys())
                            if s in st_row and st_row[s] == True]   # flipped bullish
                for sym in to_cover:
                    pos      = short_stocks.pop(sym)
                    cover_px = day_px.get(sym, pos["entry_px"])
                    if pd.isna(cover_px):
                        cover_px = pos["entry_px"]
                    pnl = pos["shares"] * (pos["entry_px"] - cover_px) * (1 - SLIPPAGE_PCT)
                    # accrue liquid on freed cash for remaining days to rebal
                    days_left  = (rebal_day - d).days
                    freed      = (pos["alloc"] + pnl) * (1 + daily_rate) ** days_left
                    cash_value    += freed
                    short_capital -= pos["alloc"]
                    total_covers  += 1

        # ── Accrue liquid fund on free cash ───────────────────────────────
        months_el  = (rebal_date - prev_date).days / 30.44
        cash_value *= (1 + monthly_rate) ** months_el

        # ── Deduct short borrow cost ───────────────────────────────────────
        if short_capital > 0:
            short_capital *= (1 - SHORT_COST_PA * (rebal_date - prev_date).days / 365)

        # ── Mark to market ─────────────────────────────────────────────────
        current_px = prices.iloc[cur_idx]
        fno_px     = fno_px_at(rebal_day)

        long_val   = sum(sh * current_px.get(s, np.nan)
                         for s, sh in long_stocks.items()
                         if not pd.isna(current_px.get(s, np.nan)))
        short_mtm  = 0.0
        for s, pos in short_stocks.items():
            cp = fno_px.get(s, pos["entry_px"])
            if pd.isna(cp):
                cp = pos["entry_px"]
            short_mtm += pos["shares"] * (pos["entry_px"] - cp)

        nav = cash_value + long_val + short_capital + short_mtm

        # ── Regime ─────────────────────────────────────────────────────────
        lb_idx    = cur_idx - LOOKBACK_DAYS
        nifty_idx = nifty_close.index.get_indexer([rebal_day], method="ffill")[0]
        n_px      = float(nifty_close.iloc[nifty_idx])
        n_ma      = float(nifty_ma100.iloc[nifty_idx])
        market_up = (not np.isnan(n_ma)) and (n_px > n_ma)

        if lb_idx >= 0:
            past_px     = prices.iloc[lb_idx]
            returns_12m = (current_px / past_px - 1).replace([np.inf, -np.inf], np.nan).dropna()
        else:
            returns_12m = pd.Series(dtype=float)

        # ── Close all longs ────────────────────────────────────────────────
        sell_val = cash_value
        for sym, shares in long_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_val += shares * p * (1 - SLIPPAGE_PCT)
        long_stocks = {}
        cash_value  = sell_val

        # ── Close all remaining shorts at month-end ────────────────────────
        for sym, pos in short_stocks.items():
            cp = fno_px.get(sym, pos["entry_px"])
            if pd.isna(cp):
                cp = pos["entry_px"]
            pnl = pos["shares"] * (pos["entry_px"] - cp) * (1 - SLIPPAGE_PCT)
            cash_value    += pos["alloc"] + pnl
            short_capital -= pos["alloc"]
        short_stocks  = {}
        short_capital = max(short_capital, 0.0)

        # ── Open new positions ─────────────────────────────────────────────
        if market_up and lb_idx >= 0:
            candidates = returns_12m.nlargest(TOP_N_LONG).index.tolist()
            candidates = [s for s in candidates if not pd.isna(current_px.get(s, np.nan))]
            if candidates:
                raw     = {s: max(returns_12m[s], 0.001) for s in candidates}
                total   = sum(raw.values())
                weights = {s: v / total for s, v in raw.items()}
                invested = 0.0
                for sym, w in weights.items():
                    p = current_px.get(sym, np.nan)
                    if pd.isna(p) or p <= 0:
                        continue
                    cost = cash_value * w * (1 + SLIPPAGE_PCT)
                    long_stocks[sym] = cost / p
                    invested += cost
                cash_value = max(cash_value - invested, 0.0)

        elif not market_up and use_short and lb_idx >= 0:
            bear_months += 1
            st_now = st_at(rebal_day)
            if st_now is not None:
                short_cands = []
                for sym in fno_cols:
                    if sym not in st_now.index:
                        continue
                    if st_now[sym] == True:        # bullish → skip
                        continue
                    p = fno_px.get(sym, np.nan)
                    if pd.isna(p) or p <= 0:
                        continue
                    if require_neg_12m:
                        r12 = returns_12m.get(sym, np.nan)
                        if pd.isna(r12) or r12 >= 0:
                            continue
                    short_cands.append(sym)

                if short_cands:
                    budget = cash_value * SHORT_ALLOC
                    cash_value   -= budget
                    short_capital = budget

                    # position sizing
                    if momentum_weighted:
                        # weight ∝ magnitude of negative 12m return (more negative = bigger short)
                        raw    = {s: abs(returns_12m.get(s, 0.001)) for s in short_cands}
                        total  = sum(raw.values())
                        allocs = {s: budget * (raw[s] / total) for s in short_cands}
                    else:
                        per_stock = budget / len(short_cands)
                        allocs = {s: per_stock for s in short_cands}

                    for sym in short_cands:
                        p      = float(fno_px[sym])
                        alloc  = allocs[sym]
                        shares = alloc * (1 - SLIPPAGE_PCT) / p
                        short_stocks[sym] = {"shares": shares, "entry_px": p, "alloc": alloc}

        nav_series[rebal_date] = nav
        prev_date = rebal_date
        prev_idx  = cur_idx

    # final NAV
    final_px  = prices.iloc[-1]
    fno_final = fno_px_at(END_DATE)
    final_nav = cash_value
    for sym, shares in long_stocks.items():
        p = final_px.get(sym, np.nan)
        if not pd.isna(p):
            final_nav += shares * p
    for sym, pos in short_stocks.items():
        cp = fno_final.get(sym, pos["entry_px"])
        if pd.isna(cp):
            cp = pos["entry_px"]
        final_nav += pos["alloc"] + pos["shares"] * (pos["entry_px"] - cp)
    nav_series[prices.index[-1]] = final_nav

    nav_s     = pd.Series(nav_series, dtype=float).sort_index()
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1/n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {
        "label": label, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
        "final_nav": nav_s.iloc[-1], "bear_months": bear_months,
        "intra_covers": total_covers, "nav_series": nav_s,
    }


# ── Run ────────────────────────────────────────────────────────────────────────
configs = [
    (False, False, False, "A) Liquid Fund (no short)"),
    (True,  False, False, "B) Supertrend only"),
    (True,  True,  False, "C) Supertrend + neg 12m return"),
    (True,  True,  True,  "D) Supertrend + neg 12m + weighted"),
]

results = []
for use_short, req_neg, mw, label in configs:
    print(f"\nRunning {label}...", end=" ", flush=True)
    r = run_backtest(use_short, req_neg, mw, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.2f}%  "
          f"BearMonths={r['bear_months']}  IntraCovers={r['intra_covers']}")

# ── Summary ────────────────────────────────────────────────────────────────────
print(f"\n{'='*92}")
print("  SHORT-SIDE STANDALONE — SUPERTREND DAILY EXIT")
print(f"{'='*92}")
print(f"  {'Config':<40} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'Final(Cr)':>10} {'Covers':>8}")
print(f"  {'-'*40} {'-'*7} {'-'*8} {'-'*8} {'-'*10} {'-'*8}")
for r in results:
    cr = r["final_nav"] / 1e7
    print(f"  {r['label']:<40} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} "
          f"{r['max_dd']*100:>7.2f}% {cr:>9.2f}  {r['intra_covers']:>7}")

print(f"\n  Supertrend : ATR({ST_ATR_PERIOD}), Multiplier={ST_MULTIPLIER}")
print(f"  Short alloc: {SHORT_ALLOC*100:.0f}% | Liquid: {(1-SHORT_ALLOC)*100:.0f}% | Bear months: {results[1]['bear_months']}")

# ── Year by year ───────────────────────────────────────────────────────────────
print(f"\n{'='*88}")
print("  YEAR-BY-YEAR")
print(f"{'='*88}")
nav_yr = [r["nav_series"].resample("YE").last() for r in results]
heads  = [r["label"][:24] for r in results]
print(f"  {'Year':<6}", end="")
for h in heads:
    print(f"  {h:>24}", end="")
print()
print(f"  {'-'*6}", end="")
for _ in heads:
    print(f"  {'-'*24}", end="")
print()

all_years = sorted(set().union(*[set(n.index.year) for n in nav_yr]))
for yr in all_years:
    rets = []
    for nav in nav_yr:
        yi = [i for i, d in enumerate(nav.index) if d.year == yr]
        rets.append((nav.iloc[yi[0]] / nav.iloc[yi[0]-1] - 1)*100 if yi and yi[0] > 0 else None)
    if all(v is not None for v in rets):
        best = max(rets)
        print(f"  {yr:<6}", end="")
        for ret in rets:
            mk = " ◄" if ret == best else "  "
            print(f"  {ret:>22.1f}%{mk}", end="")
        print()

# save
out = Path(__file__).parent / "results"
out.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:3]: r["nav_series"] for r in results}).to_csv(
    out / "short_supertrend_nav.csv")
print(f"\n  Saved → results/short_supertrend_nav.csv")
