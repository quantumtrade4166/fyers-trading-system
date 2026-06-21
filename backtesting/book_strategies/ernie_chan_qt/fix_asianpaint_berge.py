"""
fix_asianpaint_berge.py
Test multiple approaches to fix ASIANPAINT/BERGEPAINT:

  Approach 1 — Rolling ADF Gate     : only enter if last 252d spread is cointegrated
  Approach 2 — Tight HL Gate        : only enter if rolling HL < 60d
  Approach 3 — Combined Gate        : ADF + HL both must pass
  Approach 4 — Kalman Filter Beta   : dynamic hedge ratio vs static OLS
  Approach 5 — Shorter Lookback     : 126d instead of 253d (adapts faster)
  Approach 6 — Post-2024 Only       : trade only when cointegration clearly returned

Baseline = static OLS, LOOKBACK=253, MTM annual stop, no gates.
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

DATA  = Path("backtesting/book_strategies/ernie_chan_qt/data/ASIANPAINT_BERGEPAINT_daily.parquet")
data  = pd.read_parquet(DATA).dropna()
pa    = data["ASIANPAINT"].values
pb    = data["BERGEPAINT"].values
dates = data.index
n     = len(data)

LOT_A       = 200
LOT_B       = 1100
CAP         = 161_022
BROK        = 0.0003
ENTRY_Z     = 2.0
EXIT_Z      = 0.5
STOP_Z      = 4.0
ANNUAL_STOP = 82_190
COOLDOWN    = 5
LOOKBACK    = 253
SEP = "=" * 72

# ── Core backtest engine ──────────────────────────────────────────────────────
def backtest(zscores, half_lives, adf_gate=None, hl_gate=None,
             entry_z=ENTRY_Z, annual_stop=ANNUAL_STOP, label=""):
    """
    zscores, half_lives: pre-computed arrays (len n)
    adf_gate:  array of bool, True = cointegrated, False = skip entry (can be None)
    hl_gate:   max allowed rolling HL to enter (can be None = no gate)
    """
    position = 0; entry_pa = entry_pb = 0.0; entry_bar = 0
    year_pnl = 0.0; cur_yr = dates[0].year; cd_end = 0
    trades = []

    def pnl(pos, epa, epb, xpa, xpb):
        gross = ((xpa - epa)*LOT_A - (xpb - epb)*LOT_B) * pos
        costs = (epa*LOT_A + epb*LOT_B + xpa*LOT_A + xpb*LOT_B) * BROK
        return gross - costs

    for t in range(n):
        if np.isnan(zscores[t]): continue
        if dates[t].year != cur_yr: cur_yr = dates[t].year; year_pnl = 0.0
        z, hl = zscores[t], half_lives[t]

        if position != 0:
            mtm = ((pa[t]-entry_pa)*LOT_A - (pb[t]-entry_pb)*LOT_B) * position
            er  = None
            if position == 1  and z >= -EXIT_Z:        er = "z_exit"
            if position == -1 and z <= +EXIT_Z:        er = "z_exit"
            if abs(z) >= STOP_Z:                        er = "z_stop"
            if (year_pnl + mtm) < -annual_stop:        er = "annual_stop"
            if er:
                net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
                year_pnl += net
                trades.append(dict(
                    entry_date=dates[entry_bar], exit_date=dates[t],
                    net_pnl=round(net, 2), exit_reason=er,
                    hold_days=(dates[t]-dates[entry_bar]).days,
                ))
                position = 0; cd_end = t + COOLDOWN
            continue

        if t < cd_end: continue
        if year_pnl < -annual_stop: continue
        if hl_gate is not None and hl > hl_gate: continue
        if adf_gate is not None and not adf_gate[t]: continue
        if z < -entry_z:  position=1;  entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t
        elif z > entry_z: position=-1; entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t

    if position != 0:
        t = n-1
        net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
        trades.append(dict(entry_date=dates[entry_bar], exit_date=dates[t],
                           net_pnl=round(net,2), exit_reason="end_of_data",
                           hold_days=(dates[t]-dates[entry_bar]).days))
    return pd.DataFrame(trades)

def metrics(df):
    if df.empty or len(df) < 2:
        return dict(pnl=0, cagr=0, sharpe=0, max_dd=0, n=0, wr=0, pf=0,
                    dep=0, dep_ret=0)
    pnl  = df["net_pnl"].sum()
    wins = (df["net_pnl"] > 0).sum()
    wr   = wins/len(df)*100
    gw   = df[df["net_pnl"]>0]["net_pnl"].sum()
    gl   = df[df["net_pnl"]<0]["net_pnl"].sum()
    pf   = gw/abs(gl) if gl != 0 else 99

    START = df["entry_date"].min(); END = df["exit_date"].max()
    idx   = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx)
    for _, tr in df.iterrows():
        if tr["exit_date"] in daily.index: daily[tr["exit_date"]] += tr["net_pnl"]
    eq   = CAP + daily.cumsum()
    nyr  = max((END-START).days/365.25, 0.1)
    cagr = ((eq.iloc[-1]/CAP)**(1/nyr)-1)*100
    dr   = daily/CAP
    sh   = (dr.mean()/dr.std()*np.sqrt(252)) if dr.std()>0 else 0
    dd   = ((eq-eq.cummax())/eq.cummax()*100).min()
    dep  = pd.Series(False, index=idx)
    for _, tr in df.iterrows():
        dep[(idx>=tr["entry_date"])&(idx<=tr["exit_date"])] = True
    dep_f   = dep.mean()
    dep_ret = (pnl/nyr)/(CAP*dep_f)*100 if dep_f > 0 else 0
    return dict(pnl=round(pnl,0), cagr=round(cagr,2), sharpe=round(sh,3),
                max_dd=round(dd,2), n=len(df), wr=round(wr,1), pf=round(pf,2),
                dep=round(dep_f*100,1), dep_ret=round(dep_ret,1))

def print_yearly(df, label):
    if df.empty: return
    df2 = df.copy(); df2["yr"] = df2["exit_date"].dt.year
    print(f"\n  Year-by-year [{label}]:")
    for yr, g in df2.groupby("yr"):
        p = g["net_pnl"].sum(); w=(g["net_pnl"]>0).sum()
        stops = (g["exit_reason"]=="annual_stop").sum()
        s = f" [{stops} annual_stop]" if stops else ""
        sign = "+" if p >= 0 else "-"
        print(f"    {yr}: {sign}Rs{abs(p):>8,.0f}  ({len(g)}tr {w}W/{len(g)-w}L){s}")

# ── Pre-compute baseline signals (static OLS, LOOKBACK=253) ──────────────────
print("\nPre-computing baseline signals...")
zs_base = np.full(n, np.nan)
hl_base = np.full(n, np.nan)
for t in range(LOOKBACK, n):
    wa, wb = pa[t-LOOKBACK:t], pb[t-LOOKBACK:t]
    try:
        _, beta = OLS(wa, add_constant(wb)).fit().params
        sp   = wa - beta*wb
        phi  = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
        hl   = -np.log(2)/np.log(1+phi) if phi < 0 else 999
        sp_t = pa[t] - beta*pb[t]
        mu, sig = sp.mean(), sp.std()
        zs_base[t] = (sp_t-mu)/sig if sig > 0 else 0.0
        hl_base[t] = hl
    except: pass

# ── Rolling ADF gate (252d window before each bar) ───────────────────────────
print("Pre-computing rolling ADF gate (252d)...")
ADF_WIN  = 252
adf_pass = np.full(n, False)
for t in range(LOOKBACK + ADF_WIN, n):
    wa, wb = pa[t-ADF_WIN:t], pb[t-ADF_WIN:t]
    try:
        _, b   = OLS(wa, add_constant(wb)).fit().params
        sp     = wa - b*wb
        stat   = adfuller(sp, autolag="AIC")[0]
        crit10 = adfuller(sp, autolag="AIC")[4]["10%"]
        adf_pass[t] = (stat < crit10)
    except: pass

# ── Shorter lookback signals (LOOKBACK=126) ───────────────────────────────────
print("Pre-computing short-lookback signals (126d)...")
LB2  = 126
zs_s = np.full(n, np.nan)
hl_s = np.full(n, np.nan)
for t in range(LB2, n):
    wa, wb = pa[t-LB2:t], pb[t-LB2:t]
    try:
        _, beta = OLS(wa, add_constant(wb)).fit().params
        sp   = wa - beta*wb
        phi  = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
        hl   = -np.log(2)/np.log(1+phi) if phi < 0 else 999
        sp_t = pa[t] - beta*pb[t]
        mu, sig = sp.mean(), sp.std()
        zs_s[t] = (sp_t-mu)/sig if sig > 0 else 0.0
        hl_s[t] = hl
    except: pass

# ── Kalman Filter Beta ────────────────────────────────────────────────────────
print("Pre-computing Kalman Filter beta...")
# State: [beta, alpha]  Observation: price_A = alpha + beta * price_B
# Simple 1D Kalman on beta only, alpha estimated as rolling mean of residuals
def kalman_signals(pa, pb, delta=1e-5):
    n2  = len(pa)
    zs  = np.full(n2, np.nan)
    hls = np.full(n2, np.nan)
    WIN = 60  # rolling window for mu/sigma of spread

    # Kalman state
    P   = 1.0       # variance of beta estimate
    Q   = delta     # process noise
    R   = 1.0       # observation noise (will adapt)
    beta_k = 0.0

    spreads = []
    betas_k = []

    for t in range(1, n2):
        # Predict
        P = P + Q

        # Update: observation = pa[t] - beta_k * pb[t]
        K      = P * pb[t] / (pb[t]**2 * P + R)
        innov  = pa[t] - beta_k * pb[t]
        beta_k = beta_k + K * innov
        P      = (1 - K * pb[t]) * P

        # Adapt R to recent residual variance
        if t > 30:
            recent_resid = np.array([pa[i] - betas_k[i-1]*pb[i]
                                     for i in range(max(0,t-30), t)])
            R = max(np.var(recent_resid), 0.01)

        sp_t = pa[t] - beta_k * pb[t]
        spreads.append(sp_t)
        betas_k.append(beta_k)

        if len(spreads) >= WIN:
            window = np.array(spreads[-WIN:])
            mu, sig = window.mean(), window.std()
            if sig > 0:
                zs[t] = (sp_t - mu) / sig

            # Rolling HL from recent spread
            if len(spreads) >= WIN + 1:
                sp_arr = np.array(spreads[-WIN:])
                try:
                    phi_k = OLS(np.diff(sp_arr), add_constant(sp_arr[:-1])).fit().params[1]
                    hl_k  = -np.log(2)/np.log(1+phi_k) if phi_k < 0 else 999
                    hls[t] = hl_k
                except: pass

    return zs, hls

zs_kalman, hl_kalman = kalman_signals(pa, pb)

# ── Run all approaches ────────────────────────────────────────────────────────
print("\nRunning all approaches...")

approaches = [
    ("Baseline (no gate)",          zs_base,   hl_base,   None,     None,   ENTRY_Z),
    ("Approach 1: ADF gate",        zs_base,   hl_base,   adf_pass, None,   ENTRY_Z),
    ("Approach 2: HL < 60d gate",   zs_base,   hl_base,   None,     60,     ENTRY_Z),
    ("Approach 3: ADF + HL < 60d",  zs_base,   hl_base,   adf_pass, 60,     ENTRY_Z),
    ("Approach 4: Kalman Filter",   zs_kalman, hl_kalman, None,     None,   ENTRY_Z),
    ("Approach 5: Lookback=126d",   zs_s,      hl_s,      None,     None,   ENTRY_Z),
    ("Approach 6: ADF + Kalman",    zs_kalman, hl_kalman, adf_pass, None,   ENTRY_Z),
]

results = []
for label, zs, hls, adf_g, hl_g, ez in approaches:
    df = backtest(zs, hls, adf_gate=adf_g, hl_gate=hl_g, entry_z=ez)
    m  = metrics(df)
    m["label"] = label
    m["df"]    = df
    results.append(m)

# ── Summary table ─────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  COMPARISON SUMMARY  (Capital = Rs{CAP:,})")
print(SEP)
print(f"  {'Approach':<34} {'Sharpe':>7} {'CAGR':>7} {'MaxDD':>8} "
      f"{'P&L':>10} {'Trades':>7} {'WR%':>6}")
print(f"  {'─'*70}")
for m in results:
    print(f"  {m['label']:<34} {m['sharpe']:>7.3f} {m['cagr']:>6.2f}%"
          f" {m['max_dd']:>7.2f}%  Rs{m['pnl']:>8,.0f} {m['n']:>7} {m['wr']:>5.1f}%")

# ── Year-by-year for best approaches ─────────────────────────────────────────
print(f"\n{SEP}")
print("  YEAR-BY-YEAR  (top 4 by Sharpe)")
print(SEP)
ranked = sorted(results, key=lambda x: x["sharpe"], reverse=True)[:4]
for m in ranked:
    print(f"\n  ── {m['label']} ──")
    print(f"     Sharpe={m['sharpe']}  CAGR={m['cagr']}%  MaxDD={m['max_dd']}%  "
          f"Trades={m['n']}  WR={m['wr']}%")
    df = m["df"]
    if df.empty: continue
    df2 = df.copy(); df2["yr"] = df2["exit_date"].dt.year
    for yr, g in df2.groupby("yr"):
        p = g["net_pnl"].sum(); w=(g["net_pnl"]>0).sum()
        stops = (g["exit_reason"]=="annual_stop").sum()
        s = " [ANNUAL STOP]" if stops else ""
        sign = "+" if p >= 0 else "-"
        print(f"     {yr}: {sign}Rs{abs(p):>8,.0f}  ({len(g)}tr {w}W/{len(g)-w}L){s}")

# ── ADF gate effectiveness ────────────────────────────────────────────────────
print(f"\n{SEP}")
print("  ADF GATE ANALYSIS  (% of bars where gate allows entry)")
print(SEP)
valid_bars = np.sum(~np.isnan(zs_base))
gate_on    = np.sum(adf_pass & ~np.isnan(zs_base))
print(f"  Total bars with z-score    : {valid_bars}")
print(f"  Bars passing ADF gate      : {gate_on}  ({gate_on/valid_bars*100:.1f}%)")
print(f"  Bars blocked by ADF gate   : {valid_bars-gate_on}  ({(valid_bars-gate_on)/valid_bars*100:.1f}%)")
print(f"\n  ADF gate ON/OFF by year:")
for yr in range(2016, 2027):
    mask = (dates.year == yr) & (~np.isnan(zs_base))
    total = mask.sum()
    if total == 0: continue
    on = (adf_pass[mask]).sum()
    bar = "█" * int(on/total*20) + "░" * (20-int(on/total*20))
    print(f"    {yr}: [{bar}] {on/total*100:.0f}% open")

# ── Insight ───────────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print("  INSIGHT")
print(SEP)
best = ranked[0]
print(f"\n  Best approach  : {best['label']}")
print(f"  Sharpe         : {best['sharpe']}  (vs Baseline {results[0]['sharpe']})")
print(f"  CAGR           : {best['cagr']}%")
print(f"  MaxDD          : {best['max_dd']}%")
print(f"  Net P&L        : Rs{best['pnl']:,}")
print(f"  Trades         : {best['n']}  (vs Baseline {results[0]['n']})")
