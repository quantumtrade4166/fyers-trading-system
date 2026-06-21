"""
Dual Momentum V9 — Daily scan short loss cap (realistic)

During bear months, checks short book P&L every day.
If loss exceeds SHORT_LOSS_CAP % of short_capital → cover shorts immediately,
park proceeds in liquid fund for rest of that month.

Compares:
  A) Liquid Fund only (no short) — benchmark
  B) Short + month-end cap 3%   — theoretical upper bound
  C) Short + daily scan 3%      — realistic
  D) Short + daily scan 5%      — realistic looser cap
  E) Short + no cap             — worst case
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

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
TOP_N_SHORT    = 30
SHORT_ALLOC    = 0.30
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
SHORT_COST_PA  = 0.01
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"

daily_rate  = (1 + LIQUID_FUND_PA) ** (1/365) - 1
monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
short_daily_cost = SHORT_COST_PA / 365

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
fno_cols   = [s for s in FNO_SYMBOLS if s in prices.columns]
prices_fno = prices[fno_cols]

monthly_ends = prices.resample("ME").last().index
print(f"Price matrix : {prices.shape[0]} days x {prices.shape[1]} symbols")
print(f"F&O universe : {len(fno_cols)} symbols for shorting\n")


def run_no_short():
    """Baseline: long momentum + liquid fund during bear, no shorts."""
    nav_series  = {monthly_ends[0]: CAPITAL}
    cash_value  = CAPITAL
    long_stocks = {}
    prev_date   = monthly_ends[0]

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date     = prices.index[idx]
        current_px     = prices.iloc[idx]
        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        long_val = sum(sh * current_px.get(s, np.nan)
                       for s, sh in long_stocks.items()
                       if not pd.isna(current_px.get(s, np.nan)))
        nav = cash_value + long_val

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            nav_series[rebal_date] = nav
            prev_date = rebal_date
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        market_up = nifty.iloc[nifty_idx] > nifty_ma100.iloc[nifty_idx]
        returns_12m = (current_px / prices.iloc[lb_idx] - 1).dropna()

        sell_val = cash_value + sum(
            sh * current_px.get(s, np.nan) * (1 - SLIPPAGE_PCT)
            for s, sh in long_stocks.items()
            if not pd.isna(current_px.get(s, np.nan)))
        long_stocks = {}
        cash_value  = sell_val

        if market_up:
            candidates = returns_12m.nlargest(TOP_N_LONG).index.tolist()
            raw = {s: max(returns_12m[s], 0.001) for s in candidates}
            total = sum(raw.values())
            weights = {s: v/total for s, v in raw.items()}
            invested = 0
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = cash_value * w * (1 + SLIPPAGE_PCT)
                long_stocks[sym] = cost / p
                invested += cost
            cash_value = max(cash_value - invested, 0)

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    final_nav = cash_value + sum(
        sh * prices[s].dropna().iloc[-1]
        for s, sh in long_stocks.items() if s in prices.columns)
    nav_series[prices.index[-1]] = final_nav

    nav_s = pd.Series(nav_series).sort_index()
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")]
    return nav_s


def run_short_monthend_cap(cap_pct):
    """Month-end cap — theoretical upper bound (previous approach)."""
    nav_series    = {monthly_ends[0]: CAPITAL}
    cash_value    = CAPITAL
    long_stocks   = {}
    short_entry   = {}
    short_capital = 0.0
    prev_date     = monthly_ends[0]
    cap_hits      = 0

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date     = prices.index[idx]
        current_px     = prices.iloc[idx]
        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        long_val = sum(sh * current_px.get(s, np.nan)
                       for s, sh in long_stocks.items()
                       if not pd.isna(current_px.get(s, np.nan)))

        short_pnl = 0.0
        if short_entry and short_capital > 0:
            n = len(short_entry)
            for sym, ep in short_entry.items():
                cp = current_px.get(sym, np.nan)
                if pd.isna(cp) or ep <= 0:
                    continue
                short_pnl += -(cp/ep - 1) * (short_capital / n)
            short_pnl -= short_capital * (SHORT_COST_PA/12) * months_elapsed
            if cap_pct is not None and short_pnl < -cap_pct * short_capital:
                short_pnl = -cap_pct * short_capital
                cap_hits += 1

        nav = cash_value + long_val + short_capital + short_pnl

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            nav_series[rebal_date] = nav
            prev_date = rebal_date
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        market_up = nifty.iloc[nifty_idx] > nifty_ma100.iloc[nifty_idx]
        returns_12m = (current_px / prices.iloc[lb_idx] - 1).dropna()
        curr_fno    = prices_fno.iloc[idx]
        returns_fno = (curr_fno / prices_fno.iloc[lb_idx] - 1).dropna()

        sell_val = cash_value + sum(
            sh * current_px.get(s, np.nan) * (1 - SLIPPAGE_PCT)
            for s, sh in long_stocks.items()
            if not pd.isna(current_px.get(s, np.nan)))
        long_stocks = {}
        cash_value  = sell_val

        if short_entry and short_capital > 0:
            cash_value   += short_capital + short_pnl
            short_entry   = {}
            short_capital = 0.0

        if market_up:
            candidates = returns_12m.nlargest(TOP_N_LONG).index.tolist()
            raw = {s: max(returns_12m[s], 0.001) for s in candidates}
            total = sum(raw.values())
            weights = {s: v/total for s, v in raw.items()}
            invested = 0
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = cash_value * w * (1 + SLIPPAGE_PCT)
                long_stocks[sym] = cost / p
                invested += cost
            cash_value = max(cash_value - invested, 0)
        else:
            short_cands = returns_fno.nsmallest(TOP_N_SHORT).index.tolist()
            short_cands = [s for s in short_cands
                           if not pd.isna(curr_fno.get(s, np.nan)) and curr_fno.get(s, 0) > 0]
            if short_cands:
                short_capital = cash_value * SHORT_ALLOC
                cash_value   -= short_capital
                short_entry   = {s: float(curr_fno[s]) for s in short_cands}

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    final_nav = cash_value + sum(
        sh * prices[s].dropna().iloc[-1]
        for s, sh in long_stocks.items() if s in prices.columns)
    if short_entry and short_capital > 0:
        n = len(short_entry)
        fp = sum(-(prices[s].dropna().iloc[-1]/ep - 1) * (short_capital/n)
                 for s, ep in short_entry.items() if s in prices.columns)
        if cap_pct is not None:
            fp = max(fp, -cap_pct * short_capital)
        final_nav += short_capital + fp
    nav_series[prices.index[-1]] = final_nav

    nav_s = pd.Series(nav_series).sort_index()
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")]
    return nav_s, cap_hits


def run_short_daily_cap(cap_pct):
    """
    Daily scan: check short book P&L every trading day.
    If loss > cap_pct of short_capital → cover at that day's price, park in liquid.
    cap_pct=None means no cap (run to month-end).
    """
    nav_series    = {monthly_ends[0]: CAPITAL}
    cash_value    = CAPITAL
    long_stocks   = {}
    short_entry   = {}     # sym -> entry_price
    short_capital = 0.0    # cash locked as margin
    short_covered_day = None  # day index when shorts were covered intra-month
    prev_idx      = prices.index.get_indexer([monthly_ends[0]], method="ffill")[0]
    cap_hits      = 0

    for rebal_date in monthly_ends[1:]:
        cur_idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if cur_idx < 0:
            continue
        rebal_date = prices.index[cur_idx]

        # ── Daily scan between prev rebal and this rebal ───────────────────
        if short_entry and short_capital > 0 and cap_pct is not None:
            n = len(short_entry)
            for d_idx in range(prev_idx + 1, cur_idx + 1):
                day_px = prices_fno.iloc[d_idx]
                daily_pnl = 0.0
                valid = 0
                for sym, ep in short_entry.items():
                    cp = day_px.get(sym, np.nan)
                    if pd.isna(cp) or ep <= 0:
                        continue
                    daily_pnl += -(cp/ep - 1) * (short_capital / n)
                    valid += 1
                if valid < n * 0.5:
                    continue  # skip days with too many missing prices
                # deduct borrow cost so far
                days_held = d_idx - prev_idx
                daily_pnl -= short_capital * short_daily_cost * days_held

                if daily_pnl < -cap_pct * short_capital:
                    # cover shorts at this day's price with slippage
                    realised_pnl = daily_pnl * (1 - SLIPPAGE_PCT)
                    cash_value  += short_capital + realised_pnl
                    short_entry  = {}
                    short_capital = 0.0
                    cap_hits    += 1
                    # remaining days in liquid fund
                    days_remaining = (rebal_date - prices.index[d_idx]).days
                    cash_value *= (1 + daily_rate) ** days_remaining
                    short_covered_day = d_idx
                    break

        # ── Accrue liquid fund on free cash for the full month ─────────────
        # (if short was covered intra-month, cash was already accrued above)
        if short_covered_day is None:
            days_in_month = (rebal_date - prices.index[prev_idx]).days
            cash_value   *= (1 + daily_rate) ** days_in_month

        # ── Mark to market at month-end ────────────────────────────────────
        current_px = prices.iloc[cur_idx]
        long_val   = sum(sh * current_px.get(s, np.nan)
                         for s, sh in long_stocks.items()
                         if not pd.isna(current_px.get(s, np.nan)))

        short_pnl = 0.0
        if short_entry and short_capital > 0:
            n = len(short_entry)
            for sym, ep in short_entry.items():
                cp = current_px.get(sym, np.nan)
                if pd.isna(cp) or ep <= 0:
                    continue
                short_pnl += -(cp/ep - 1) * (short_capital / n)
            days_held = (rebal_date - prices.index[prev_idx]).days
            short_pnl -= short_capital * short_daily_cost * days_held

        nav = cash_value + long_val + short_capital + short_pnl

        # ── Absolute filter ────────────────────────────────────────────────
        lb_idx = cur_idx - LOOKBACK_DAYS
        if lb_idx < 0:
            nav_series[rebal_date] = nav
            prev_idx = cur_idx
            short_covered_day = None
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        market_up = nifty.iloc[nifty_idx] > nifty_ma100.iloc[nifty_idx]
        returns_12m = (current_px / prices.iloc[lb_idx] - 1).dropna()
        curr_fno    = prices_fno.iloc[cur_idx]
        returns_fno = (curr_fno / prices_fno.iloc[lb_idx] - 1).dropna()

        # close longs
        sell_val = cash_value + sum(
            sh * current_px.get(s, np.nan) * (1 - SLIPPAGE_PCT)
            for s, sh in long_stocks.items()
            if not pd.isna(current_px.get(s, np.nan)))
        long_stocks = {}
        cash_value  = sell_val

        # close any remaining shorts at month-end
        if short_entry and short_capital > 0:
            cash_value   += short_capital + short_pnl
            short_entry   = {}
            short_capital = 0.0

        # open new positions
        if market_up:
            candidates = returns_12m.nlargest(TOP_N_LONG).index.tolist()
            raw = {s: max(returns_12m[s], 0.001) for s in candidates}
            total = sum(raw.values())
            weights = {s: v/total for s, v in raw.items()}
            invested = 0
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0:
                    continue
                cost = cash_value * w * (1 + SLIPPAGE_PCT)
                long_stocks[sym] = cost / p
                invested += cost
            cash_value = max(cash_value - invested, 0)
        else:
            short_cands = returns_fno.nsmallest(TOP_N_SHORT).index.tolist()
            short_cands = [s for s in short_cands
                           if not pd.isna(curr_fno.get(s, np.nan)) and curr_fno.get(s, 0) > 0]
            if short_cands:
                short_capital = cash_value * SHORT_ALLOC
                cash_value   -= short_capital
                short_entry   = {s: float(curr_fno[s]) for s in short_cands}

        nav_series[rebal_date] = nav
        prev_idx = cur_idx
        short_covered_day = None

    final_nav = cash_value + sum(
        sh * prices[s].dropna().iloc[-1]
        for s, sh in long_stocks.items() if s in prices.columns)
    nav_series[prices.index[-1]] = final_nav

    nav_s = pd.Series(nav_series).sort_index()
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")]
    return nav_s, cap_hits


def stats(nav_s):
    r = nav_s.pct_change().dropna()
    n = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr   = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1/n) - 1
    sharpe = r.mean() / r.std() * np.sqrt(12) if r.std() > 0 else 0
    maxdd  = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()
    return cagr, sharpe, maxdd


# ── Run all configs ────────────────────────────────────────────────────────────
print("Running A) Liquid Fund (no short)...", end=" ", flush=True)
nav_lf = run_no_short()
ca, sa, da = stats(nav_lf)
print(f"CAGR={ca*100:.2f}%  Sharpe={sa:.3f}  MaxDD={da*100:.2f}%")

print("Running B) Short + month-end cap 3% (theoretical)...", end=" ", flush=True)
nav_me3, hits_me3 = run_short_monthend_cap(0.03)
cb, sb, db = stats(nav_me3)
print(f"CAGR={cb*100:.2f}%  Sharpe={sb:.3f}  MaxDD={db*100:.2f}%  CapHits={hits_me3}")

print("Running C) Short + daily scan cap 3% (realistic)...", end=" ", flush=True)
nav_d3, hits_d3 = run_short_daily_cap(0.03)
cc, sc, dc = stats(nav_d3)
print(f"CAGR={cc*100:.2f}%  Sharpe={sc:.3f}  MaxDD={dc*100:.2f}%  CapHits={hits_d3}")

print("Running D) Short + daily scan cap 5% (realistic)...", end=" ", flush=True)
nav_d5, hits_d5 = run_short_daily_cap(0.05)
cd, sd, dd = stats(nav_d5)
print(f"CAGR={cd*100:.2f}%  Sharpe={sd:.3f}  MaxDD={dd*100:.2f}%  CapHits={hits_d5}")

print("Running E) Short + no cap...", end=" ", flush=True)
nav_nc, hits_nc = run_short_daily_cap(None)
ce, se, de = stats(nav_nc)
print(f"CAGR={ce*100:.2f}%  Sharpe={se:.3f}  MaxDD={de*100:.2f}%")

# ── Summary table ──────────────────────────────────────────────────────────────
configs = [
    ("A) ★ Liquid Fund (no short)",        ca, sa, da, 0,        nav_lf,  "benchmark"),
    ("B) Month-end cap 3% [theoretical]",  cb, sb, db, hits_me3, nav_me3, "upper bound"),
    ("C) Daily scan cap 3% [realistic]",   cc, sc, dc, hits_d3,  nav_d3,  "realistic"),
    ("D) Daily scan cap 5% [realistic]",   cd, sd, dd, hits_d5,  nav_d5,  "realistic"),
    ("E) No cap",                          ce, se, de, 0,        nav_nc,  "worst case"),
]

print(f"\n{'='*95}")
print("  DUAL MOMENTUM V9 — DAILY SCAN vs MONTH-END CAP COMPARISON")
print(f"{'='*95}")
print(f"  {'Config':<40} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'Final (Cr)':>11} {'CapHits':>8}")
print(f"  {'-'*40} {'-'*7} {'-'*8} {'-'*8} {'-'*11} {'-'*8}")
for lbl, cagr, sharpe, maxdd, hits, _, note in configs:
    cr = (CAPITAL * (1+cagr)**20) / 1e7  # approximate 20yr final
    actual_cr = configs[0][5].iloc[-1] / 1e7
    real_cr = _.iloc[-1] / 1e7
    print(f"  {lbl:<40} {cagr*100:>6.2f}% {sharpe:>8.3f} {maxdd*100:>7.2f}% {real_cr:>10.2f}  {hits:>7}  [{note}]")

print(f"\n  Key insight:")
print(f"  Month-end cap 3%  CAGR = {cb*100:.2f}% ← theoretical (cap applied retroactively)")
print(f"  Daily scan cap 3% CAGR = {cc*100:.2f}% ← realistic (cover when loss hits 3% intraday)")
print(f"  Difference = {(cb-cc)*100:.2f}% CAGR — this is the 'look-ahead gap'")

# ── Year-by-year table ─────────────────────────────────────────────────────────
print(f"\n{'='*105}")
print("  YEAR-BY-YEAR COMPARISON")
print(f"{'='*105}")
nav_list  = [nav_lf, nav_me3, nav_d3, nav_d5, nav_nc]
col_heads = ["LiqFund", "ME-cap3%", "Daily3%", "Daily5%", "NoCap"]
print(f"  {'Year':<6}", end="")
for h in col_heads:
    print(f"  {h:>10}", end="")
print(f"  {'Best':>10}")
print(f"  {'-'*6}", end="")
for _ in col_heads:
    print(f"  {'-'*10}", end="")
print(f"  {'-'*10}")

all_years = sorted(set().union(*[set(n.index.year) for n in nav_list]))
for yr in all_years:
    rets = []
    for nav in nav_list:
        yi = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yi and yi[0] > 0:
            rets.append((nav.iloc[yi[0]] / nav.iloc[yi[0]-1] - 1) * 100)
        else:
            rets.append(None)
    if all(v is not None for v in rets):
        best_lbl = col_heads[rets.index(max(rets))]
        print(f"  {yr:<6}", end="")
        for r in rets:
            print(f"  {r:>9.1f}%", end="")
        print(f"  {best_lbl:>10}")

# save NAVs
out = Path(__file__).parent / "results"
out.mkdir(exist_ok=True)
pd.DataFrame({
    "LiqFund": nav_lf, "ME_cap3pct": nav_me3,
    "Daily_cap3pct": nav_d3, "Daily_cap5pct": nav_d5, "NoCap": nav_nc
}).to_csv(out / "dual_momentum_v9_daily_cap.csv")
print(f"\n  NAV saved → {out / 'dual_momentum_v9_daily_cap.csv'}")
