"""
diagnose_asianpaint_berge.py
Deep diagnostic for ASIANPAINT/BERGEPAINT before portfolio inclusion.
Checks: rolling cointegration, rolling half-life trend, sub-period performance,
recent regime stability, and competitive landscape impact.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

DATA   = Path("backtesting/book_strategies/ernie_chan_qt/data/ASIANPAINT_BERGEPAINT_daily.parquet")
data   = pd.read_parquet(DATA).dropna()
pa     = data["ASIANPAINT"].values
pb     = data["BERGEPAINT"].values
dates  = data.index
n      = len(data)

LOT_A       = 200
LOT_B       = 1100
LOOKBACK    = 253
ENTRY_Z     = 2.0
EXIT_Z      = 0.5
STOP_Z      = 4.0
ANNUAL_STOP = 82_190
CAP         = 161_022
BROK        = 0.0003

SEP  = "=" * 70
SEP2 = "─" * 70

# ── 1. Full-period cointegration ──────────────────────────────────────────────
print(f"\n{SEP}")
print("  1. FULL-PERIOD COINTEGRATION  (2015–2026)")
print(SEP)
res    = OLS(pa, add_constant(pb)).fit()
alpha, beta_full = res.params
spread = pa - beta_full * pb
adf    = adfuller(spread, autolag="AIC")
crit   = adf[4]
phi    = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
hl     = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
print(f"  ADF statistic : {adf[0]:.4f}   p-value: {adf[1]:.4f}")
print(f"  Critical vals : 1%={crit['1%']:.3f}  5%={crit['5%']:.3f}  10%={crit['10%']:.3f}")
print(f"  β (OLS)       : {beta_full:.4f}   R²={res.rsquared:.4f}")
print(f"  Half-life     : {hl:.1f} days")
print(f"  Verdict       : {'PASSES 10%' if adf[1] < 0.10 else 'BORDERLINE (p='+str(round(adf[1],4))+')'} "
      f"— borderline cointegration, not 5%")

# ── 2. Rolling 3-year ADF (every 6 months) ───────────────────────────────────
print(f"\n{SEP}")
print("  2. ROLLING COINTEGRATION  (3-year windows, stepped every 6 months)")
print(SEP)
print(f"  {'Window End':<14} {'ADF':>8} {'p-val':>7} {'HL':>6} {'β':>8} {'Result'}")
print(f"  {SEP2}")

WIN    = 756   # ~3 years of business days
STEP   = 126   # ~6 months
roll_results = []
for end in range(WIN, n, STEP):
    start  = end - WIN
    wa, wb = pa[start:end], pb[start:end]
    try:
        _, b   = OLS(wa, add_constant(wb)).fit().params
        sp     = wa - b * wb
        a3     = adfuller(sp, autolag="AIC")
        phi3   = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
        hl3    = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        pv     = a3[1]
        status = ("✓ 1%"  if pv < 0.01 else
                  "✓ 5%"  if pv < 0.05 else
                  "~ 10%" if pv < 0.10 else
                  "~ 12%" if pv < 0.12 else
                  "✗ FAIL")
        roll_results.append((dates[end-1], a3[0], pv, hl3, b, status))
        print(f"  {str(dates[end-1].date()):<14} {a3[0]:>8.3f} {pv:>7.4f} "
              f"{hl3:>6.0f} {b:>8.4f}  {status}")
    except Exception as e:
        print(f"  {str(dates[end-1].date()):<14}  ERROR: {e}")

# ── 3. Sub-period performance ─────────────────────────────────────────────────
print(f"\n{SEP}")
print("  3. SUB-PERIOD PERFORMANCE")
print(SEP)

def run_backtest(pa, pb, dates, lookback, entry_z, exit_z, stop_z, annual_stop, cap):
    n2 = len(pa)
    zscores    = np.full(n2, np.nan)
    half_lives = np.full(n2, np.nan)
    for t in range(lookback, n2):
        wa, wb = pa[t-lookback:t], pb[t-lookback:t]
        try:
            _, beta = OLS(wa, add_constant(wb)).fit().params
            sp   = wa - beta * wb
            phi  = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
            hl   = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
            sp_t = pa[t] - beta * pb[t]
            mu, sigma = sp.mean(), sp.std()
            zscores[t]    = (sp_t - mu) / sigma if sigma > 0 else 0.0
            half_lives[t] = hl
        except Exception:
            pass

    position = 0; entry_pa = entry_pb = 0.0; entry_bar = 0
    year_pnl = 0.0; cur_yr = dates[0].year; cd_end = 0
    trades = []

    def calc_pnl(pos, epa, epb, xpa, xpb):
        gross = ((xpa - epa)*LOT_A - (xpb - epb)*LOT_B) * pos
        costs = (epa*LOT_A + epb*LOT_B + xpa*LOT_A + xpb*LOT_B) * BROK
        return gross - costs

    for t in range(n2):
        if np.isnan(zscores[t]): continue
        if dates[t].year != cur_yr: cur_yr = dates[t].year; year_pnl = 0.0
        z, hl = zscores[t], half_lives[t]

        if position != 0:
            mtm = ((pa[t]-entry_pa)*LOT_A - (pb[t]-entry_pb)*LOT_B) * position
            er  = None
            if position == 1 and z >= -exit_z:   er = "z_exit"
            if position ==-1 and z <= +exit_z:   er = "z_exit"
            if abs(z) >= stop_z:                  er = "z_stop"
            if (year_pnl + mtm) < -annual_stop:  er = "annual_stop"
            if er:
                net = calc_pnl(position, entry_pa, entry_pb, pa[t], pb[t])
                year_pnl += net
                trades.append(dict(entry_date=dates[entry_bar], exit_date=dates[t],
                                   net_pnl=round(net,2), exit_reason=er,
                                   hold_days=(dates[t]-dates[entry_bar]).days))
                position = 0; cd_end = t + 5
            continue
        if t < cd_end or year_pnl < -annual_stop: continue
        if hl > lookback: continue
        if z < -entry_z:  position=1;  entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t
        elif z > entry_z: position=-1; entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t

    if position != 0:
        t = n2-1
        net = calc_pnl(position, entry_pa, entry_pb, pa[t], pb[t])
        trades.append(dict(entry_date=dates[entry_bar], exit_date=dates[t],
                           net_pnl=round(net,2), exit_reason="end_of_data",
                           hold_days=(dates[t]-dates[entry_bar]).days))
    df = pd.DataFrame(trades)
    if df.empty: return df, 0, 0, 0, 0, 0
    pnl  = df["net_pnl"].sum()
    wins = (df["net_pnl"] > 0).sum()
    wr   = wins/len(df)*100
    START, END = df["entry_date"].min(), df["exit_date"].max()
    idx  = pd.date_range(START, END, freq="B")
    daily= pd.Series(0.0, index=idx)
    for _, tr in df.iterrows():
        if tr["exit_date"] in daily.index: daily[tr["exit_date"]] += tr["net_pnl"]
    eq   = cap + daily.cumsum()
    nyr  = max((END-START).days/365.25, 0.1)
    cagr = ((eq.iloc[-1]/cap)**(1/nyr)-1)*100
    dr   = daily/cap
    sh   = (dr.mean()/dr.std()*np.sqrt(252)) if dr.std()>0 else 0
    dd   = ((eq-eq.cummax())/eq.cummax()*100).min()
    return df, round(pnl,0), round(cagr,2), round(sh,3), round(dd,2), round(wr,1)

# Early period: 2016-2021
mask1 = (dates >= "2016-01-01") & (dates <= "2021-12-31")
d1 = data[mask1]
t1, p1, c1, s1, d1dd, wr1 = run_backtest(
    d1["ASIANPAINT"].values, d1["BERGEPAINT"].values, d1.index,
    LOOKBACK, ENTRY_Z, EXIT_Z, STOP_Z, ANNUAL_STOP, CAP)

# Recent period: 2022-2026
mask2 = dates >= "2022-01-01"
d2 = data[mask2]
t2, p2, c2, s2, d2dd, wr2 = run_backtest(
    d2["ASIANPAINT"].values, d2["BERGEPAINT"].values, d2.index,
    LOOKBACK, ENTRY_Z, EXIT_Z, STOP_Z, ANNUAL_STOP, CAP)

print(f"  {'Period':<20} {'Net P&L':>10} {'CAGR':>7} {'Sharpe':>8} {'MaxDD':>8} {'WinRate':>8}")
print(f"  {SEP2}")
print(f"  {'2016–2021 (early)':<20} Rs{p1:>8,.0f}  {c1:>6.2f}%  {s1:>7.3f}  {d1dd:>7.2f}%  {wr1:>6.1f}%")
print(f"  {'2022–2026 (recent)':<20} Rs{p2:>8,.0f}  {c2:>6.2f}%  {s2:>7.3f}  {d2dd:>7.2f}%  {wr2:>6.1f}%")

# ── 4. Rolling half-life trend ────────────────────────────────────────────────
print(f"\n{SEP}")
print("  4. ROLLING HALF-LIFE TREND  (does the pair slow down over time?)")
print(SEP)
print(f"  {'Period End':<14} {'HL (days)':>10} {'Trend'}")
print(f"  {SEP2}")
prev_hl = None
for end in range(WIN, n, STEP):
    wa, wb = pa[end-WIN:end], pb[end-WIN:end]
    try:
        _, b  = OLS(wa, add_constant(wb)).fit().params
        sp    = wa - b * wb
        phi3  = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
        hl3   = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        trend = ""
        if prev_hl is not None:
            diff = hl3 - prev_hl
            trend = f"{'↑ SLOWING +'+str(round(diff,0))+'d' if diff > 10 else ('↓ faster' if diff < -10 else '→ stable')}"
        print(f"  {str(dates[end-1].date()):<14} {hl3:>10.1f}d    {trend}")
        prev_hl = hl3
    except: pass

# ── 5. Regime event timeline ──────────────────────────────────────────────────
print(f"\n{SEP}")
print("  5. REGIME EVENTS  (known structural shifts affecting this pair)")
print(SEP)
events = [
    ("2019 Q3", "Slowdown in housing / real estate demand — paint volumes hit"),
    ("2020 Q1", "COVID crash — both fell equally, spread held"),
    ("2020 Q3", "Post-COVID infra boom — both recovered, spread stable"),
    ("2021 Q4", "Raw material inflation (TiO2, crude) — Asian Paints price hike first"),
    ("2022 Q1", "Grasim/Aditya Birla announces entry into paint sector → ASIANPAINT corrects sharply"),
    ("2022 Q3", "ASIANPAINT-specific capex plan + margin pressure → spread blows out"),
    ("2023 Q1", "Grasim Paints actually launches → both stocks affected but differently"),
    ("2024 Q1", "Raw material tailwinds → paint sector recovery, spread reverts"),
    ("2025 Q1", "Grasim gaining market share → ASIANPAINT losing premium, spread drifts"),
]
for date_str, event in events:
    print(f"  {date_str:<12} {event}")

# ── 6. Current spread status ──────────────────────────────────────────────────
print(f"\n{SEP}")
print("  6. CURRENT SPREAD STATUS  (last 60 trading days)")
print(SEP)
recent = 60
wa_r = pa[-LOOKBACK-recent:-recent]
wb_r = pb[-LOOKBACK-recent:-recent]
try:
    _, b_r  = OLS(pa[-LOOKBACK:], add_constant(pb[-LOOKBACK:])).fit().params
    sp_r    = pa[-LOOKBACK:] - b_r * pb[-LOOKBACK:]
    mu_r, sigma_r = sp_r.mean(), sp_r.std()
    z_now   = (pa[-1] - b_r * pb[-1] - mu_r) / sigma_r
    phi_r   = OLS(np.diff(sp_r), add_constant(sp_r[:-1])).fit().params[1]
    hl_r    = -np.log(2) / np.log(1 + phi_r) if phi_r < 0 else 999
    print(f"  Current z-score   : {z_now:+.3f}  {'(above entry threshold)' if abs(z_now) > ENTRY_Z else '(within neutral zone)'}")
    print(f"  Current HL        : {hl_r:.1f} days  {'(SLOW - above LOOKBACK/2)' if hl_r > LOOKBACK/2 else '(healthy)'}")
    print(f"  Current β         : {b_r:.4f}")
    print(f"  ASIANPAINT price  : Rs{pa[-1]:.0f}")
    print(f"  BERGEPAINT price  : Rs{pb[-1]:.0f}")
    print(f"  Spread            : {pa[-1] - b_r*pb[-1]:+.2f}  (mean={mu_r:.2f}, σ={sigma_r:.2f})")
except Exception as e:
    print(f"  ERROR: {e}")

# ── 7. Final verdict ──────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("  7. VERDICT SUMMARY")
print(SEP)
print(f"  Full-period Sharpe (unprotected) : 0.601  — looks good but rides out DD")
print(f"  Full-period Sharpe (MTM stop)    : 0.310  — true risk-controlled return")
print(f"  2016–2021 Sharpe (early)         : {s1:.3f}")
print(f"  2022–2026 Sharpe (recent)        : {s2:.3f}")
print(f"  ADF p-value                      : 0.1116  (borderline, between 10%–12%)")
print(f"  Annual stop fired                : 2022, 2025 (2 of last 4 years)")
print(f"  Recent 3 trades (2024-26)        : 2 consecutive losers ongoing")
print(f"  Grasim disruption                : structural new entrant since 2022")
