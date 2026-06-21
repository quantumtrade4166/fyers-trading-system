import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
from pathlib import Path
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

DATA_DIR = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")

CANDIDATES = [
    # (name, sym_A, sym_B)
    # Pharma
    ("CIPLA/DRREDDY",        "CIPLA",      "DRREDDY"),
    ("SUNPHARMA/DIVISLAB",   "SUNPHARMA",  "DIVISLAB"),
    ("LUPIN/AUROPHARMA",     "LUPIN",      "AUROPHARMA"),
    ("LUPIN/CIPLA",          "LUPIN",      "CIPLA"),
    ("GLENMARK/CIPLA",       "GLENMARK",   "CIPLA"),
    # PSU Oil
    ("BPCL/IOC",             "BPCL",       "IOC"),
    ("BPCL/HINDPETRO",       "BPCL",       "HINDPETRO"),
    ("IOC/HINDPETRO",        "IOC",        "HINDPETRO"),
    # Metals / Mining
    ("COALINDIA/NMDC",       "COALINDIA",  "NMDC"),
    ("HINDALCO/JSWSTEEL",    "HINDALCO",   "JSWSTEEL"),
    ("HINDALCO/SAIL",        "HINDALCO",   "SAIL"),
    ("JSWSTEEL/SAIL",        "JSWSTEEL",   "SAIL"),
    # FMCG / Consumer
    ("GODREJCP/MARICO",      "GODREJCP",   "MARICO"),
    ("PIDILITIND/ASIANPAINT","PIDILITIND",  "ASIANPAINT"),
    # Jewellery
    ("TITAN/KALYANKJIL",     "TITAN",       "KALYANKJIL"),
    # Gold NBFC
    ("MUTHOOTFIN/MANAPPURAM","MUTHOOTFIN",  "MANAPPURAM"),
    # Capital Goods
    ("SIEMENS/ABB",          "SIEMENS",     "ABB"),
    # PSU Banks
    ("BANKBARODA/PNB",       "BANKBARODA",  "PNB"),
]

START = "2015-01-01"
END   = "2026-06-01"


def load(symbol):
    f = DATA_DIR / f"{symbol}.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df.loc[START:END].copy()
    if "close" in df.columns:
        col = "close"
    elif "Close" in df.columns:
        col = "Close"
    else:
        return None
    out = df[[col]].rename(columns={col: symbol})
    out = out[~out.index.duplicated(keep="last")]
    return out


def half_life(spread):
    s = pd.Series(spread)
    lag = s.shift(1).dropna()
    ds = s.diff().dropna()
    ols = OLS(ds, add_constant(lag)).fit()
    phi = ols.params.iloc[1]
    if phi >= 0:
        return np.nan
    return -np.log(2) / np.log(1 + phi)


def hurst(ts):
    lags = range(2, 21)
    tau = [np.std(np.subtract(ts[lag:], ts[:-lag])) for lag in lags]
    if any(t == 0 for t in tau):
        return np.nan
    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    return poly[0]


def screen(name, sym_a, sym_b):
    da = load(sym_a)
    db = load(sym_b)
    if da is None or db is None:
        return {"Pair": name, "N": 0, "Beta": "—", "ADF_p": 99, "ADF_stat": 99,
                "Half-life": "—", "Lookback": "—", "Hurst": "—", "Pass": "❌ no data"}
    df = da.join(db, how="inner").dropna()
    if len(df) < 252:
        return {"Pair": name, "N": len(df), "Beta": "—", "ADF_p": 99, "ADF_stat": 99,
                "Half-life": "—", "Lookback": "—", "Hurst": "—", "Pass": "❌ too short"}

    la = np.log(df[sym_a].values)
    lb = np.log(df[sym_b].values)

    ols = OLS(lb, add_constant(la)).fit()
    beta = float(ols.params[1])
    spread = lb - beta * la

    adf = adfuller(spread, autolag="AIC")
    pval = adf[1]
    stat = adf[0]

    hl = half_life(spread)
    lookback = max(int(2 * hl), 63) if not np.isnan(hl) else np.nan
    h = hurst(spread)

    ok_pval = pval < 0.05
    ok_hl   = not np.isnan(hl) and 20 <= hl <= 200
    ok_hurst = not np.isnan(h) and h < 0.5
    warn_pval = 0.05 <= pval < 0.10

    if ok_pval and ok_hl and ok_hurst:
        flag = "✅"
    elif (ok_pval or warn_pval) and ok_hl:
        flag = "⚠️"
    else:
        flag = "❌"

    return {
        "Pair": name,
        "N": len(df),
        "Beta": round(beta, 3),
        "ADF_stat": round(stat, 3),
        "ADF_p": round(pval, 4),
        "Half-life": round(hl, 1) if not np.isnan(hl) else "—",
        "Lookback": int(lookback) if not np.isnan(lookback) else "—",
        "Hurst": round(h, 3) if not np.isnan(h) else "—",
        "Pass": flag,
    }


print("Screening candidates...")
results = [screen(n, a, b) for n, a, b in CANDIDATES]

# Sort by ADF p-value
results_sorted = sorted(results, key=lambda r: r["ADF_p"] if isinstance(r["ADF_p"], float) else 99)

print("\n" + "=" * 95)
print("PAIR SCREENING RESULTS  (ADF p<0.05 = cointegrated | Half-life 20-200d | Hurst<0.5 = mean-reverting)")
print("=" * 95)
print(f"  {'':2} {'Pair':<26} {'N':>5} {'Beta':>7} {'ADF_stat':>9} {'ADF_p':>7} {'HL(d)':>7} {'Lookback':>9} {'Hurst':>7}")
print("  " + "-" * 80)
for r in results_sorted:
    print(f"  {r['Pass']:2} {r['Pair']:<26} {r['N']:>5} {str(r['Beta']):>7} "
          f"{str(r['ADF_stat']):>9} {str(r['ADF_p']):>7} {str(r['Half-life']):>7} "
          f"{str(r['Lookback']):>9} {str(r['Hurst']):>7}")

print()
pass_pairs = [r for r in results if r["Pass"] == "✅"]
warn_pairs = [r for r in results if r["Pass"] == "⚠️"]
print(f"PASS  (p<0.05, HL 20-200d, Hurst<0.5): {len(pass_pairs)}")
for p in pass_pairs:
    print(f"  → {p['Pair']:<28} ADF_p={p['ADF_p']}  HL={p['Half-life']}d  Hurst={p['Hurst']}")
print(f"BORDERLINE (p<0.10 or HL borderline):  {len(warn_pairs)}")
for p in warn_pairs:
    print(f"  → {p['Pair']:<28} ADF_p={p['ADF_p']}  HL={p['Half-life']}d  Hurst={p['Hurst']}")
