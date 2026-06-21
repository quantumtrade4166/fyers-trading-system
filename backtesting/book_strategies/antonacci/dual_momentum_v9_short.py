"""
Dual Momentum V9 — Short side during bear market (clean implementation)

When Nifty > 100MA → Long top 50 momentum stocks (momentum weighted, full portfolio)
When Nifty < 100MA → Short bottom 30 worst F&O stocks (30% of portfolio)
                      Park 70% in liquid fund (6% p.a.)

Short P&L logic (simple):
  - At entry, record entry prices for bottom 30
  - At next month-end, compute return on each short = -(price_change%)
  - Apply to 30% allocation
  - Short borrow cost: 1% p.a. deducted from short gains

Compares:
  A) Long only + Liquid Fund (baseline)
  B) Long + Short during bear
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf

DATA_DIR       = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")

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
LIQUID_ALLOC   = 0.70
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
SHORT_COST_PA  = 0.01
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
fno_cols   = [s for s in FNO_SYMBOLS if s in prices.columns]
prices_fno = prices[fno_cols]

monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
short_monthly_cost = SHORT_COST_PA / 12
print(f"Price matrix : {prices.shape[0]} days x {prices.shape[1]} symbols")
print(f"F&O universe : {len(fno_cols)} symbols for shorting\n")


def run_backtest(allow_short, label):
    nav_series    = {monthly_ends[0]: CAPITAL}
    cash_value    = CAPITAL
    long_stocks   = {}   # sym -> shares
    short_entry   = {}   # sym -> entry_price (at start of bear month)
    short_capital = 0.0  # amount allocated to short book
    prev_date     = monthly_ends[0]
    months_short  = 0
    months_bear   = 0

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date     = prices.index[idx]
        current_px     = prices.iloc[idx]
        months_elapsed = (rebal_date - prev_date).days / 30.44

        # accrue liquid fund on free cash
        cash_value *= (1 + monthly_rate) ** months_elapsed

        # ── Mark to market ────────────────────────────────────────────────────
        long_val = sum(sh * current_px.get(s, np.nan)
                       for s, sh in long_stocks.items()
                       if not pd.isna(current_px.get(s, np.nan)))

        # short P&L: equal-weight short return for the month
        short_pnl = 0.0
        if short_entry and short_capital > 0:
            n_short = len(short_entry)
            for sym, ep in short_entry.items():
                cp = current_px.get(sym, np.nan)
                if pd.isna(cp) or ep <= 0:
                    continue
                stock_ret = (cp / ep) - 1          # stock return (positive = stock up = loss for short)
                short_pnl += -(stock_ret) * (short_capital / n_short)  # short earns negative of stock return
            # deduct borrow cost
            short_pnl -= short_capital * short_monthly_cost * months_elapsed

        nav = cash_value + long_val + short_capital + short_pnl

        # ── Absolute filter ────────────────────────────────────────────────────
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
        returns_12m = (current_px / past_px - 1).dropna()
        past_fno    = prices_fno.iloc[lb_idx]
        curr_fno    = prices_fno.iloc[idx]
        returns_fno = (curr_fno / past_fno - 1).dropna()

        # ── Close longs ───────────────────────────────────────────────────────
        sell_val = cash_value
        for sym, shares in long_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_val += shares * p * (1 - SLIPPAGE_PCT)
        long_stocks = {}
        cash_value  = sell_val

        # ── Close shorts — realise P&L ─────────────────────────────────────
        if short_entry and short_capital > 0:
            realised = short_capital + short_pnl  # return margin + P&L
            cash_value   += realised
            short_entry   = {}
            short_capital = 0.0

        # ── Open new positions ────────────────────────────────────────────────
        if market_up:
            candidates = returns_12m.nlargest(TOP_N_LONG).index.tolist()
            if candidates:
                raw     = {s: max(returns_12m[s], 0.001) for s in candidates}
                total   = sum(raw.values())
                weights = {s: v / total for s, v in raw.items()}
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
            months_bear += 1
            if allow_short:
                months_short += 1
                short_cands = returns_fno.nsmallest(TOP_N_SHORT).index.tolist()
                short_cands = [s for s in short_cands
                               if not pd.isna(curr_fno.get(s, np.nan))
                               and curr_fno.get(s, 0) > 0]
                if short_cands:
                    short_capital = cash_value * SHORT_ALLOC
                    cash_value   -= short_capital  # lock margin, rest stays in liquid
                    short_entry   = {s: float(curr_fno[s]) for s in short_cands}

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    # final NAV
    final_px  = prices.iloc[-1]
    final_nav = cash_value
    for sym, shares in long_stocks.items():
        p = final_px.get(sym, np.nan)
        if not pd.isna(p):
            final_nav += shares * p
    if short_entry and short_capital > 0:
        n_short = len(short_entry)
        final_short_pnl = 0.0
        for sym, ep in short_entry.items():
            cp = final_px.get(sym, np.nan)
            if not pd.isna(cp) and ep > 0:
                final_short_pnl += -(cp / ep - 1) * (short_capital / n_short)
        final_nav += short_capital + final_short_pnl
    nav_series[prices.index[-1]] = final_nav

    nav_s     = pd.Series(nav_series).sort_index()
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {"label": label, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
            "final_nav": nav_s.iloc[-1], "bear_months": months_bear,
            "short_months": months_short, "nav_series": nav_s}


configs = [
    (False, "A) Long only + Liquid Fund"),
    (True,  "B) Long + Short (30%) in bear"),
]

results = []
for allow_short, label in configs:
    print(f"  Running {label}...", end=" ", flush=True)
    r = run_backtest(allow_short, label)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.2f}%  FinalNAV=Rs {r['final_nav']:,.0f}")

print("\n" + "=" * 78)
print("  DUAL MOMENTUM V9 — LONG ONLY vs LONG + SHORT DURING BEAR")
print("=" * 78)
print(f"  {'Config':<35} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV':>16}")
print(f"  {'-'*35} {'-'*7} {'-'*8} {'-'*8} {'-'*16}")
for r in results:
    print(f"  {r['label']:<35} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.2f}% {r['final_nav']:>16,.0f}")
print("=" * 78)
print(f"\n  Bear months (Nifty < 100MA) : {results[0]['bear_months']}")
print(f"  Months we went short        : {results[1]['short_months']}")
boost = results[1]['cagr'] - results[0]['cagr']
print(f"  Short side CAGR boost       : {boost*100:+.2f}%")

# year-by-year
print(f"\n  {'Year':<6} {'LongOnly%':>10} {'LongOnly PnL':>14} | {'Long+Short%':>12} {'L+S PnL':>14} {'Winner':>8}")
print(f"  {'-'*6} {'-'*10} {'-'*14}   {'-'*12} {'-'*14} {'-'*8}")
navs = [r["nav_series"].resample("YE").last() for r in results]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    rets, pnls = [], []
    for nav in navs:
        yi = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yi and yi[0] > 0:
            rets.append((nav.iloc[yi[0]] / nav.iloc[yi[0]-1] - 1) * 100)
            pnls.append(nav.iloc[yi[0]] - nav.iloc[yi[0]-1])
        else:
            rets.append(None); pnls.append(None)
    if all(v is not None for v in rets):
        winner = "SHORT" if rets[1] > rets[0] else "LONG "
        print(f"  {yr:<6} {rets[0]:>9.1f}% {pnls[0]:>14,.0f} | {rets[1]:>11.1f}% {pnls[1]:>14,.0f} {winner:>8}")

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
pd.DataFrame({r["label"][:1]: r["nav_series"] for r in results}).to_csv(out_dir / "dual_momentum_v9_nav.csv")
print(f"\n  NAV saved → {out_dir / 'dual_momentum_v9_nav.csv'}")
