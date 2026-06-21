"""
screen_new_pairs.py
Download + cointegration screen for 8 new candidate pairs.
Yahoo Finance 2015-2024 + Fyers 2024-2026, stitched to daily OHLCV.
Reports: ADF level, half-life, lot balance, margin estimate.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
import yfinance as yf
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

OUT    = Path("backtesting/book_strategies/ernie_chan_qt/data")
OUT.mkdir(parents=True, exist_ok=True)
loader = DataLoader()
cutoff = pd.Timestamp("2024-05-27")

# (label, yf_A, fyers_A, lotA,  yf_B, fyers_B, lotB, note)
PAIRS = [
    # City gas distribution duopoly — same PNGRB tariff, mirrors NTPC/PG
    ("IGL/MGL",
     "IGL.NS",        "NSE:IGL-EQ",        1375,
     "MGL.NS",        "NSE:MGL-EQ",         400,  ""),

    # Bajaj group: BAJAJFINSV holds 52% of BAJFINANCE — tightest fundamental link
    ("BAJFINANCE/BAJAJFINSV",
     "BAJFINANCE.NS",  "NSE:BAJFINANCE-EQ",  125,
     "BAJAJFINSV.NS",  "NSE:BAJAJFINSV-EQ",  500,  ""),

    # PSU banking pair — same NPA cycle, same govt recap mandates
    ("SBIN/BANKBARODA",
     None, None, 1500,
     None, None, 3500,
     "CACHED:sbin_bob_daily.parquet:SBIN:BANKBARODA"),

    # OMC refiners — petroleum ministry pricing (different from HINDPETRO/BPCL)
    ("HINDPETRO/IOC",
     "HINDPETRO.NS",  "NSE:HINDPETRO-EQ",  2100,
     "IOC.NS",        "NSE:IOC-EQ",         2500,  ""),

    # Aluminum sector — same LME pricing, same power cost structure
    ("NATIONALUM/HINDALCO",
     "NATIONALUM.NS",  "NSE:NATIONALUM-EQ",  7100,
     "HINDALCO.NS",    "NSE:HINDALCO-EQ",    1075,  ""),

    # Pharma majors — branded formulations, similar US/India revenue mix
    ("SUNPHARMA/CIPLA",
     "SUNPHARMA.NS",  "NSE:SUNPHARMA-EQ",   700,
     "CIPLA.NS",      "NSE:CIPLA-EQ",        650,  ""),

    # Electrical cables duopoly (note: POLYCAB IPO April 2019 — shorter history)
    ("HAVELLS/POLYCAB",
     "HAVELLS.NS",    "NSE:HAVELLS-EQ",      300,
     "POLYCAB.NS",    "NSE:POLYCAB-EQ",      150,  "SHORT_HISTORY_POLYCAB_2019"),

    # IT services — similar offshore mix, similar deal sizes
    ("HCLTECH/WIPRO",
     "HCLTECH.NS",    "NSE:HCLTECH-EQ",      700,
     "WIPRO.NS",      "NSE:WIPRO-EQ",       3000,  ""),
]

results = []
SEP = "─" * 72

for entry in PAIRS:
    label = entry[0]
    note  = entry[-1]
    print(f"\n{'='*72}")
    print(f"  {label}  {'['+note+']' if note else ''}")
    print(f"{'='*72}")

    try:
        # ── Load data ─────────────────────────────────────────────────────────
        if note.startswith("CACHED:"):
            _, fname, ca, cb = note.split(":")
            data = pd.read_parquet(OUT / fname)[[ca, cb]].dropna()
            na, nb = ca, cb
            la, lb = entry[3], entry[6]
        else:
            yfa, fya, la = entry[1], entry[2], entry[3]
            yfb, fyb, lb = entry[4], entry[5], entry[6]
            na = label.split("/")[0]
            nb = label.split("/")[1]
            cache = OUT / f"{na}_{nb}_daily.parquet"

            if cache.exists():
                data = pd.read_parquet(cache).dropna()
                print(f"  Loaded from cache: {len(data)} rows")
            else:
                print(f"  Downloading Yahoo Finance...")
                dfa = yf.download(yfa, start="2015-01-01", end="2024-05-28",
                                  auto_adjust=True, progress=False)
                dfb = yf.download(yfb, start="2015-01-01", end="2024-05-28",
                                  auto_adjust=True, progress=False)
                for df in [dfa, dfb]:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                dfa.index = pd.to_datetime(dfa.index).normalize()
                dfb.index = pd.to_datetime(dfb.index).normalize()
                yf_df = pd.DataFrame({na: dfa["Close"], nb: dfb["Close"]}).dropna()
                print(f"  YF: {len(yf_df)} rows  "
                      f"({yf_df.index[0].date()} to {yf_df.index[-1].date()})")

                print(f"  Loading Fyers 2024-2026...")
                raw = loader.load_many([fya, fyb])
                fy  = {}
                for sym, df in raw.items():
                    d = resample_ohlcv(df, "1D")
                    d.index = d.index.normalize()
                    fy[na if sym == fya else nb] = d["close"]
                fy_df = pd.DataFrame(fy).dropna()
                print(f"  Fyers: {len(fy_df)} rows")

                data = pd.concat([yf_df[yf_df.index <= cutoff],
                                  fy_df[fy_df.index > cutoff]]).sort_index()
                data = data[~data.index.duplicated(keep="last")].dropna()
                data.to_parquet(cache)
                print(f"  Saved: {len(data)} rows → {cache.name}")

        if len(data) < 500:
            print(f"  SKIP — only {len(data)} rows"); continue

        pa = data[na].values
        pb = data[nb].values
        dates = data.index
        print(f"  Data: {dates[0].date()} → {dates[-1].date()}  ({len(data)} rows)")
        if note and "SHORT" in note:
            print(f"  ⚠  {note}")

        # ── OLS ───────────────────────────────────────────────────────────────
        res   = OLS(pa, add_constant(pb)).fit()
        alpha, beta = res.params
        spread = pa - beta * pb

        # ── ADF ───────────────────────────────────────────────────────────────
        adf  = adfuller(spread, autolag="AIC")
        stat, pval, crit = adf[0], adf[1], adf[4]
        level = ("1%"  if stat < crit["1%"] else
                 "5%"  if stat < crit["5%"] else
                 "10%" if stat < crit["10%"] else "FAIL")

        # ── Half-life ─────────────────────────────────────────────────────────
        phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
        hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else 999

        # ── Lot balance ───────────────────────────────────────────────────────
        shares_b  = beta * la
        lots_b    = shares_b / lb
        best_lots = max(1, round(lots_b))
        actual_lb = best_lots * lb
        imb       = abs(actual_lb - shares_b) / shares_b * 100 if shares_b > 0 else 99

        # ── Margin estimate ───────────────────────────────────────────────────
        avg_a  = pa[-252:].mean()
        avg_b  = pb[-252:].mean()
        margin = int((avg_a * la + avg_b * actual_lb) * 0.15)

        passed = stat < crit["5%"]
        border = stat < crit["10%"] and not passed

        print(f"  OLS:   β={beta:.4f}  α={alpha:.2f}  R²={res.rsquared:.3f}")
        print(f"  ADF:   stat={stat:.3f}  p={pval:.4f}  [{level}]"
              f"  {'✓ COINTEGRATED' if passed else ('~ BORDERLINE' if border else '✗ NOT COINTEGRATED')}")
        print(f"  HL:    {hl:.1f} days  →  LOOKBACK = {int(hl*2)} days")
        print(f"  Lots:  {la} {na} : β×{la}={shares_b:.0f} {nb} shares"
              f" → {best_lots} lot(s) = {actual_lb:.0f} shares  imb={imb:.1f}%"
              f"  {'✓' if imb <= 40 else '✗ TOO HIGH'}")
        print(f"  Capital: ~Rs{margin:,}  (15% SPAN margin)")

        # ── Rolling HL check ──────────────────────────────────────────────────
        print(f"  Rolling HL (252d windows):", end="")
        hl_vals = []
        for t in range(252, len(spread), 252):
            s = spread[t-252:t]
            try:
                phi3 = OLS(np.diff(s), add_constant(s[:-1])).fit().params[1]
                hl3  = -np.log(2)/np.log(1+phi3) if phi3 < 0 else 999
                hl_vals.append(hl3)
                print(f"  {dates[t].year}:{hl3:.0f}d", end="")
            except: pass
        print()
        hl_stable = max(hl_vals) < 3 * hl if hl_vals and hl < 300 else False
        print(f"  HL stability: {'✓ STABLE' if hl_stable else '⚠ VARIABLE'}")

        results.append(dict(
            pair=label, stat=round(stat,3), pval=round(pval,4),
            level=level, passed=passed, border=border,
            hl=round(hl,0), lookback=int(hl*2),
            beta=round(beta,4), lots_b=round(lots_b,2),
            imb=round(imb,1), margin=margin,
            r2=round(res.rsquared,3),
            tradeable=(passed or border) and imb <= 40 and hl < 300,
            note=note,
        ))

    except Exception as e:
        import traceback
        print(f"  ERROR: {e}")
        traceback.print_exc()

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n\n{'='*72}")
print(f"  SCREENING SUMMARY  (sorted by ADF stat)")
print(f"{'='*72}")
print(f"  {'Pair':<26} {'ADF':>7} {'p':>6} {'Lvl':>5} {'HL':>5} {'LB':>5} "
      f"{'imb':>5} {'margin':>9} {'R²':>5}  Result")
print(f"  {'─'*70}")
for r in sorted(results, key=lambda x: x["stat"]):
    mark = ("✓✓" if r["level"] in ("1%",) else
            "✓"  if r["level"] == "5%" else
            "~"  if r["border"] else "✗")
    imb_flag = "!" if r["imb"] > 40 else ""
    print(f"  {mark} {r['pair']:<24} {r['stat']:>7.3f} {r['pval']:>6.4f} "
          f"{r['level']:>5} {r['hl']:>5.0f} {r['lookback']:>5} "
          f"{r['imb']:>4.0f}%{imb_flag} Rs{r['margin']:>7,} {r['r2']:>5.3f}")

tradeable = [r for r in results if r["tradeable"]]
print(f"\n  PAIRS TO BACKTEST: {len(tradeable)}/{len(results)}")
for r in tradeable:
    flag = " ⚠ SHORT HISTORY" if "SHORT" in r.get("note","") else ""
    print(f"    → {r['pair']:<26} ADF={r['level']}  HL={r['hl']:.0f}d  "
          f"margin~Rs{r['margin']:,}{flag}")
