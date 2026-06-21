"""
adf_gate_all_pairs.py
Apply rolling ADF gate to ALL pairs and compare gated vs ungated performance.
Pairs tested: TCS/INFY, NTPC/POWERGRID, ASIANPAINT/BERGEPAINT,
              HINDPETRO/BPCL, COALINDIA/NMDC, MRF/APOLLOTYRE
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

DATA_DIR = Path("backtesting/book_strategies/ernie_chan_qt/data")
BROK     = 0.0003
ADF_WIN  = 252   # 1-year rolling window for ADF gate
COOLDOWN = 5

# ── Pair configs — (label, file, colA, lotA, colB, lotB) ─────────────────────
# Parameters from pair_optimizer calibration
PAIRS = [
    dict(label="TCS/INFY",
         file="tcs_infy_daily_2015_2024.parquet",
         colA="TCS",  lotA=150, colB="INFY", lotB=600,
         lookback=126, entry_z=2.0, stop_z=3.5, annual_stop=58_000,
         cap=200_000),
    dict(label="NTPC/POWERGRID",
         file="ntpc_powergrid_daily_2015_2024.parquet",
         colA="NTPC", lotA=3250, colB="POWERGRID", lotB=4200,
         lookback=252, entry_z=2.0, stop_z=3.5, annual_stop=80_000,
         cap=300_000),
    dict(label="ASIANPAINT/BERGEPAINT",
         file="ASIANPAINT_BERGEPAINT_daily.parquet",
         colA="ASIANPAINT", lotA=200, colB="BERGEPAINT", lotB=1100,
         lookback=253, entry_z=2.0, stop_z=4.0, annual_stop=82_190,
         cap=161_022),
    dict(label="HINDPETRO/BPCL",
         file="HINDPETRO_BPCL_daily.parquet",
         colA="HINDPETRO", lotA=2100, colB="BPCL", lotB=3600,
         lookback=141, entry_z=1.5, stop_z=4.0, annual_stop=72_603,
         cap=312_962),
    dict(label="COALINDIA/NMDC",
         file="COALINDIA_NMDC_daily.parquet",
         colA="COALINDIA", lotA=4200, colB="NMDC", lotB=18000,
         lookback=247, entry_z=2.5, stop_z=4.0, annual_stop=266_246,
         cap=470_977),
    dict(label="MRF/APOLLOTYRE",
         file="MRF_APOLLOTYRE_daily.parquet",
         colA="MRF", lotA=24, colB="APOLLOTYRE", lotB=5500,
         lookback=200, entry_z=2.5, stop_z=4.0, annual_stop=460_885,
         cap=899_282),
]

EXIT_Z = 0.5
SEP  = "=" * 76
SEP2 = "─" * 76

# ── Core engine ───────────────────────────────────────────────────────────────
def compute_signals(pa, pb, lookback):
    n = len(pa)
    zs  = np.full(n, np.nan)
    hls = np.full(n, np.nan)
    for t in range(lookback, n):
        wa, wb = pa[t-lookback:t], pb[t-lookback:t]
        try:
            _, beta = OLS(wa, add_constant(wb)).fit().params
            sp   = wa - beta*wb
            phi  = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
            hl   = -np.log(2)/np.log(1+phi) if phi < 0 else 999
            sp_t = pa[t] - beta*pb[t]
            mu, sig = sp.mean(), sp.std()
            zs[t]  = (sp_t-mu)/sig if sig > 0 else 0.0
            hls[t] = hl
        except: pass
    return zs, hls

def compute_adf_gate(pa, pb, lookback):
    n = len(pa)
    gate = np.full(n, False)
    for t in range(lookback + ADF_WIN, n):
        wa, wb = pa[t-ADF_WIN:t], pb[t-ADF_WIN:t]
        try:
            _, b  = OLS(wa, add_constant(wb)).fit().params
            sp    = wa - b*wb
            res   = adfuller(sp, autolag="AIC")
            gate[t] = res[0] < res[4]["10%"]
        except: pass
    return gate

def run_sim(pa, pb, dates, zs, hls, cfg, use_gate, gate):
    lot_a = cfg["lotA"]; lot_b = cfg["lotB"]
    entry_z = cfg["entry_z"]; stop_z = cfg["stop_z"]
    annual_stop = cfg["annual_stop"]
    max_hl = cfg["lookback"]  # same as lookback

    def pnl(pos, epa, epb, xpa, xpb):
        gross = ((xpa-epa)*lot_a - (xpb-epb)*lot_b) * pos
        costs = (epa*lot_a+epb*lot_b+xpa*lot_a+xpb*lot_b) * BROK
        return gross - costs

    position = 0; entry_pa = entry_pb = 0.0; entry_bar = 0
    year_pnl = 0.0; cur_yr = dates[0].year; cd_end = 0
    trades = []

    for t in range(len(pa)):
        if np.isnan(zs[t]): continue
        if dates[t].year != cur_yr: cur_yr = dates[t].year; year_pnl = 0.0
        z, hl = zs[t], hls[t]

        if position != 0:
            mtm = ((pa[t]-entry_pa)*lot_a - (pb[t]-entry_pb)*lot_b) * position
            er  = None
            if position == 1  and z >= -EXIT_Z:       er = "z_exit"
            if position == -1 and z <= +EXIT_Z:       er = "z_exit"
            if abs(z) >= stop_z:                       er = "z_stop"
            if (year_pnl + mtm) < -annual_stop:       er = "annual_stop"
            if er:
                net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
                year_pnl += net
                trades.append(dict(
                    entry_date=dates[entry_bar], exit_date=dates[t],
                    net_pnl=round(net,2), exit_reason=er,
                    hold_days=(dates[t]-dates[entry_bar]).days,
                ))
                position = 0; cd_end = t + COOLDOWN
            continue

        if t < cd_end: continue
        if year_pnl < -annual_stop: continue
        if hl > max_hl: continue
        if use_gate and not gate[t]: continue
        if z < -entry_z:  position=1;  entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t
        elif z > entry_z: position=-1; entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t

    if position != 0:
        t = len(pa)-1
        net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
        trades.append(dict(entry_date=dates[entry_bar], exit_date=dates[t],
                           net_pnl=round(net,2), exit_reason="end_of_data",
                           hold_days=(dates[t]-dates[entry_bar]).days))
    return pd.DataFrame(trades)

def calc_metrics(df, cap):
    if df.empty or len(df) < 2:
        return dict(pnl=0,cagr=0,sharpe=0,max_dd=0,n=0,wr=0,pf=0)
    pnl  = df["net_pnl"].sum()
    wins = (df["net_pnl"]>0).sum()
    wr   = wins/len(df)*100
    gw   = df[df["net_pnl"]>0]["net_pnl"].sum()
    gl   = df[df["net_pnl"]<0]["net_pnl"].sum()
    pf   = gw/abs(gl) if gl != 0 else 99
    START = df["entry_date"].min(); END = df["exit_date"].max()
    idx   = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx)
    for _, tr in df.iterrows():
        if tr["exit_date"] in daily.index: daily[tr["exit_date"]] += tr["net_pnl"]
    eq   = cap + daily.cumsum()
    nyr  = max((END-START).days/365.25, 0.1)
    try: cagr = ((eq.iloc[-1]/cap)**(1/nyr)-1)*100
    except: cagr = 0
    dr   = daily/cap
    sh   = (dr.mean()/dr.std()*np.sqrt(252)) if dr.std()>0 else 0
    dd   = ((eq-eq.cummax())/eq.cummax()*100).min()
    return dict(pnl=round(pnl,0),cagr=round(cagr,2),sharpe=round(sh,3),
                max_dd=round(dd,2),n=len(df),wr=round(wr,1),pf=round(pf,2))

# ── Main loop ─────────────────────────────────────────────────────────────────
summary = []

for cfg in PAIRS:
    print(f"\n{SEP}")
    print(f"  {cfg['label']}")
    print(SEP)

    fpath = DATA_DIR / cfg["file"]
    if not fpath.exists():
        print(f"  File not found: {fpath}"); continue

    data = pd.read_parquet(fpath).dropna()
    # handle column name variations
    colA, colB = cfg["colA"], cfg["colB"]
    if colA not in data.columns:
        # try uppercase
        data.columns = [c.upper() for c in data.columns]
    if colA not in data.columns:
        print(f"  Column {colA} not found. Available: {list(data.columns)}"); continue

    pa    = data[colA].values
    pb    = data[colB].values
    dates = data.index
    cap   = cfg["cap"]

    print(f"  Data: {dates[0].date()} → {dates[-1].date()}  ({len(data)} rows)")
    print(f"  Computing signals (lookback={cfg['lookback']})...", end=" ", flush=True)
    zs, hls = compute_signals(pa, pb, cfg["lookback"])
    print("done")
    print(f"  Computing ADF gate (252d windows)...", end=" ", flush=True)
    gate = compute_adf_gate(pa, pb, cfg["lookback"])
    print("done")

    # Gate stats
    valid = np.sum(~np.isnan(zs))
    open_pct = gate[~np.isnan(zs)].mean() * 100
    print(f"  ADF gate open: {open_pct:.1f}% of tradeable bars")

    # Run both versions
    df_no  = run_sim(pa, pb, dates, zs, hls, cfg, use_gate=False, gate=gate)
    df_yes = run_sim(pa, pb, dates, zs, hls, cfg, use_gate=True,  gate=gate)
    m_no   = calc_metrics(df_no,  cap)
    m_yes  = calc_metrics(df_yes, cap)

    # Delta
    sh_delta  = m_yes["sharpe"] - m_no["sharpe"]
    pnl_delta = m_yes["pnl"]    - m_no["pnl"]

    print(f"\n  {'Metric':<18} {'No Gate':>12} {'ADF Gate':>12} {'Delta':>10}")
    print(f"  {SEP2[:54]}")
    for k, label in [("sharpe","Sharpe"),("cagr","CAGR%"),("max_dd","MaxDD%"),
                     ("pnl","Net P&L"),("n","Trades"),("wr","WinRate%")]:
        v_no  = m_no[k];  v_yes = m_yes[k]
        if k == "pnl":
            s_no = f"Rs{v_no:>8,.0f}"; s_yes = f"Rs{v_yes:>8,.0f}"
            s_d  = f"Rs{pnl_delta:>+8,.0f}"
        else:
            s_no  = f"{v_no:>12}"; s_yes = f"{v_yes:>12}"
            s_d   = f"{(v_yes-v_no):>+10.3f}" if isinstance(v_yes,(int,float)) else ""
        print(f"  {label:<18} {s_no:>12} {s_yes:>12} {s_d:>10}")

    verdict_gate = ("HELPS" if sh_delta > 0.05 else
                    "NEUTRAL" if abs(sh_delta) <= 0.05 else "HURTS")
    print(f"\n  Gate verdict: {verdict_gate}  (Sharpe {sh_delta:+.3f})")

    # Year-by-year comparison
    print(f"\n  Year-by-year  (No Gate vs ADF Gate):")
    print(f"  {'Year':<6} {'No Gate P&L':>14} {'ADF Gate P&L':>14} {'Diff':>12}")
    print(f"  {'─'*50}")

    all_years = set()
    if not df_no.empty:  all_years |= set(df_no["exit_date"].dt.year.unique())
    if not df_yes.empty: all_years |= set(df_yes["exit_date"].dt.year.unique())
    for yr in sorted(all_years):
        p_no  = df_no[df_no["exit_date"].dt.year==yr]["net_pnl"].sum() if not df_no.empty else 0
        p_yes = df_yes[df_yes["exit_date"].dt.year==yr]["net_pnl"].sum() if not df_yes.empty else 0
        diff  = p_yes - p_no
        n_no  = len(df_no[df_no["exit_date"].dt.year==yr]) if not df_no.empty else 0
        n_yes = len(df_yes[df_yes["exit_date"].dt.year==yr]) if not df_yes.empty else 0
        s_no  = f"Rs{p_no:>+9,.0f}({n_no}tr)"
        s_yes = f"Rs{p_yes:>+9,.0f}({n_yes}tr)" if p_yes != 0 or n_yes > 0 else f"{'BLOCKED':>14}"
        s_d   = f"Rs{diff:>+8,.0f}" if diff != 0 else "—"
        print(f"  {yr:<6} {s_no:>16} {s_yes:>16} {s_d:>12}")

    # Current gate status
    last_gate = gate[-1]
    recent_open_pct = gate[-60:].mean()*100 if len(gate) >= 60 else 0
    print(f"\n  Current gate status : {'OPEN ✓' if last_gate else 'CLOSED ✗'}")
    print(f"  Last 60 days open   : {recent_open_pct:.0f}%")

    summary.append(dict(
        label=cfg["label"],
        sh_no=m_no["sharpe"], sh_yes=m_yes["sharpe"], delta=round(sh_delta,3),
        pnl_no=m_no["pnl"],   pnl_yes=m_yes["pnl"],
        n_no=m_no["n"],        n_yes=m_yes["n"],
        gate_open=f"{open_pct:.0f}%",
        verdict=verdict_gate,
        gate_now="OPEN" if last_gate else "CLOSED",
    ))

# ── Final summary ─────────────────────────────────────────────────────────────
print(f"\n\n{SEP}")
print(f"  FINAL SUMMARY — ADF Gate Impact Across All Pairs")
print(SEP)
print(f"  {'Pair':<26} {'Sh(no)':>7} {'Sh(gate)':>9} {'Delta':>7} "
      f"{'Gate%':>6} {'Now':>7} {'Verdict'}")
print(f"  {SEP2}")
for r in summary:
    delta_str = f"{r['delta']:>+7.3f}"
    print(f"  {r['label']:<26} {r['sh_no']:>7.3f}  {r['sh_yes']:>8.3f} "
          f"{delta_str:>7} {r['gate_open']:>6} {r['gate_now']:>7}  {r['verdict']}")
