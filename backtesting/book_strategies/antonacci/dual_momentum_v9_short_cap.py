"""
Dual Momentum V9 — Short loss cap sweep
Tests short book loss caps from 3% to 25% to find sweet spot.
Cap is applied at month-end: if short book lost > cap%, loss is limited to cap%.
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

monthly_ends       = prices.resample("ME").last().index
monthly_rate       = (1 + LIQUID_FUND_PA) ** (1/12) - 1
short_monthly_cost = SHORT_COST_PA / 12
print(f"Price matrix : {prices.shape[0]} days x {prices.shape[1]} symbols")
print(f"F&O universe : {len(fno_cols)} symbols for shorting\n")


def run_backtest(short_loss_cap, label):
    """
    short_loss_cap: max loss allowed on short book as fraction of short_capital
                    e.g. 0.10 = cap loss at 10% of short book size
                    None = no cap (baseline)
    """
    nav_series    = {monthly_ends[0]: CAPITAL}
    cash_value    = CAPITAL
    long_stocks   = {}
    short_entry   = {}
    short_capital = 0.0
    prev_date     = monthly_ends[0]
    cap_hits      = 0

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0:
            continue
        rebal_date     = prices.index[idx]
        current_px     = prices.iloc[idx]
        months_elapsed = (rebal_date - prev_date).days / 30.44

        cash_value *= (1 + monthly_rate) ** months_elapsed

        long_val = sum(sh * current_px.get(s, np.nan)
                       for s, sh in long_stocks.items()
                       if not pd.isna(current_px.get(s, np.nan)))

        # short P&L
        short_pnl = 0.0
        if short_entry and short_capital > 0:
            n_short = len(short_entry)
            raw_pnl = 0.0
            for sym, ep in short_entry.items():
                cp = current_px.get(sym, np.nan)
                if pd.isna(cp) or ep <= 0:
                    continue
                stock_ret = (cp / ep) - 1
                raw_pnl += -(stock_ret) * (short_capital / n_short)
            raw_pnl -= short_capital * short_monthly_cost * months_elapsed

            if short_loss_cap is not None:
                floor = -short_loss_cap * short_capital
                if raw_pnl < floor:
                    raw_pnl = floor
                    cap_hits += 1
            short_pnl = raw_pnl

        nav = cash_value + long_val + short_capital + short_pnl

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

        # close longs
        sell_val = cash_value
        for sym, shares in long_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_val += shares * p * (1 - SLIPPAGE_PCT)
        long_stocks = {}
        cash_value  = sell_val

        # close shorts
        if short_entry and short_capital > 0:
            cash_value   += short_capital + short_pnl
            short_entry   = {}
            short_capital = 0.0

        # open new positions
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
            short_cands = returns_fno.nsmallest(TOP_N_SHORT).index.tolist()
            short_cands = [s for s in short_cands
                           if not pd.isna(curr_fno.get(s, np.nan))
                           and curr_fno.get(s, 0) > 0]
            if short_cands:
                short_capital = cash_value * SHORT_ALLOC
                cash_value   -= short_capital
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
        fp = 0.0
        for sym, ep in short_entry.items():
            cp = final_px.get(sym, np.nan)
            if not pd.isna(cp) and ep > 0:
                fp += -(cp / ep - 1) * (short_capital / n_short)
        if short_loss_cap is not None:
            fp = max(fp, -short_loss_cap * short_capital)
        final_nav += short_capital + fp
    nav_series[prices.index[-1]] = final_nav

    nav_s     = pd.Series(nav_series).sort_index()
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()

    return {
        "label": label, "cagr": cagr, "sharpe": sharpe,
        "max_dd": max_dd, "final_nav": nav_s.iloc[-1],
        "cap_hits": cap_hits, "nav_series": nav_s
    }


# ── Run sweep ─────────────────────────────────────────────────────────────────
caps = [None, 0.03, 0.05, 0.07, 0.10, 0.12, 0.15, 0.20, 0.25]
labels = {
    None:  "No cap (baseline short)",
    0.03:  "3% cap",
    0.05:  "5% cap",
    0.07:  "7% cap",
    0.10:  "10% cap",
    0.12:  "12% cap",
    0.15:  "15% cap",
    0.20:  "20% cap",
    0.25:  "25% cap",
}

# also run pure liquid fund baseline (no short)
print("Running pure liquid fund baseline (no short)...")
baseline = run_backtest(None, "LIQUID FUND (no short)")

# monkey-patch: for baseline we need to disable shorts entirely
# re-run with a flag
def run_no_short():
    nav_series  = {monthly_ends[0]: CAPITAL}
    cash_value  = CAPITAL
    long_stocks = {}
    prev_date   = monthly_ends[0]

    for i, rebal_date in enumerate(monthly_ends[1:], 1):
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
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()

        sell_val = cash_value
        for sym, shares in long_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_val += shares * p * (1 - SLIPPAGE_PCT)
        long_stocks = {}
        cash_value  = sell_val

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

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    final_px  = prices.iloc[-1]
    final_nav = cash_value
    for sym, shares in long_stocks.items():
        p = final_px.get(sym, np.nan)
        if not pd.isna(p):
            final_nav += shares * p
    nav_series[prices.index[-1]] = final_nav

    nav_s     = pd.Series(nav_series).sort_index()
    nav_s     = nav_s[~nav_s.index.duplicated(keep="last")]
    returns_m = nav_s.pct_change().dropna()
    n_years   = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr      = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / n_years) - 1
    sharpe    = returns_m.mean() / returns_m.std() * np.sqrt(12) if returns_m.std() > 0 else 0
    max_dd    = ((nav_s - nav_s.cummax()) / nav_s.cummax()).min()
    return {"label": "★ LIQUID FUND (no short)", "cagr": cagr, "sharpe": sharpe,
            "max_dd": max_dd, "final_nav": nav_s.iloc[-1], "cap_hits": 0, "nav_series": nav_s}

print("Running Liquid Fund (no short)...", end=" ", flush=True)
lf_result = run_no_short()
print(f"CAGR={lf_result['cagr']*100:.2f}%  Sharpe={lf_result['sharpe']:.3f}  MaxDD={lf_result['max_dd']*100:.2f}%")

results = [lf_result]
for cap in caps:
    lbl = labels[cap]
    print(f"Running {lbl}...", end=" ", flush=True)
    r = run_backtest(cap, lbl)
    results.append(r)
    print(f"CAGR={r['cagr']*100:.2f}%  Sharpe={r['sharpe']:.3f}  MaxDD={r['max_dd']*100:.2f}%  CapHits={r['cap_hits']}")

# ── Results table ─────────────────────────────────────────────────────────────
print(f"\n{'='*90}")
print("  DUAL MOMENTUM V9 — SHORT LOSS CAP SWEEP")
print(f"{'='*90}")
print(f"  {'Config':<28} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'FinalNAV (Cr)':>15} {'CapHits':>8}")
print(f"  {'-'*28} {'-'*7} {'-'*8} {'-'*8} {'-'*15} {'-'*8}")

best_sharpe = max(results, key=lambda x: x["sharpe"])
best_cagr   = max(results, key=lambda x: x["cagr"])

for r in results:
    flag = ""
    if r["label"] == best_sharpe["label"]:
        flag += " ◄ best Sharpe"
    if r["label"] == best_cagr["label"]:
        flag += " ◄ best CAGR"
    cr = r["final_nav"] / 1e7
    print(f"  {r['label']:<28} {r['cagr']*100:>6.2f}% {r['sharpe']:>8.3f} {r['max_dd']*100:>7.2f}% {cr:>14.2f}  {r['cap_hits']:>7}{flag}")

print(f"\n  Note: cap is on short book only (30% of portfolio). 70% stays in liquid fund.")
print(f"  Short book bear months: 93  |  CAPITAL: Rs 10L  |  Period: 2006-2026")

# ── Year by year for top 3 configs ────────────────────────────────────────────
top3 = [lf_result] + sorted(results[1:], key=lambda x: x["sharpe"], reverse=True)[:3]
print(f"\n{'='*100}")
print("  YEAR-BY-YEAR: Liquid Fund vs top 3 capped configs")
print(f"{'='*100}")
hdrs = [r["label"][:18] for r in top3]
print(f"  {'Year':<6}", end="")
for h in hdrs:
    print(f" {h:>20}", end="")
print()
print(f"  {'-'*6}", end="")
for _ in hdrs:
    print(f" {'-'*20}", end="")
print()

navs = [r["nav_series"].resample("YE").last() for r in top3]
all_years = sorted(set().union(*[set(n.index.year) for n in navs]))
for yr in all_years:
    print(f"  {yr:<6}", end="")
    for nav in navs:
        yi = [i for i, d in enumerate(nav.index) if d.year == yr]
        if yi and yi[0] > 0:
            ret = (nav.iloc[yi[0]] / nav.iloc[yi[0]-1] - 1) * 100
            print(f" {ret:>19.1f}%", end="")
        else:
            print(f" {'—':>20}", end="")
    print()
