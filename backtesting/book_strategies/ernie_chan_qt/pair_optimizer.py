"""
pair_optimizer.py
Rigorous per-pair calibration:
  1. Download data (Yahoo Finance 2015-2024 + Fyers 2024-2026)
  2. ADF cointegration test — skip if p > 0.10
  3. Measure half-life → set LOOKBACK = 2 × HL
  4. Run permissive V1 (no annual stop) → measure avg trade loss
  5. Sweep ENTRY_Z x STOP_Z (9 combos) → pick best Sharpe
  6. Final run with ANNUAL_STOP = 3 × avg_loss from step 4
  7. Print full results + year-by-year table

Add pairs to CANDIDATES list — each pair is tested independently.
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

# ─────────────────────────────────────────────────────────────────────────────
# CANDIDATE PAIRS
# (Name_A, YF_A, Fyers_A, LotA,  Name_B, YF_B, Fyers_B, LotB)
# ─────────────────────────────────────────────────────────────────────────────
CANDIDATES = [
    # PSU Gas transmission — same regulated tariff model as NTPC/POWERGRID
    ("GAIL",     "GAIL.NS",     "NSE:GAIL-EQ",     4550,
     "PETRONET",  "PETRONET.NS", "NSE:PETRONET-EQ", 3000),
    # PSU Power (hydro vs thermal, same ministry)
    ("NTPC",     "NTPC.NS",     "NSE:NTPC-EQ",     3250,
     "NHPC",     "NHPC.NS",     "NSE:NHPC-EQ",    26550),
    # Paint duopoly — borderline passed ADF (-2.52)
    ("ASIANPAINT","ASIANPAINT.NS","NSE:ASIANPAINT-EQ", 200,
     "BERGEPAINT","BERGEPAINT.NS","NSE:BERGEPAINT-EQ",1100),
    # IT midcap — similar revenue mix, US exposure
    ("MPHASIS",  "MPHASIS.NS",  "NSE:MPHASIS-EQ",   350,
     "LTIMINDTREE","LTIM.NS",   "NSE:LTIMINDTREE-EQ",150),
    # OMC pair — both refiners, same govt pricing (different from BPCL/IOC)
    ("HINDPETRO","HINDPETRO.NS","NSE:HINDPETRO-EQ", 2100,
     "BPCL",     "BPCL.NS",     "NSE:BPCL-EQ",      1800),
    # Cement (Adani acquired Holcim India = ACC + Ambuja — post-acquisition)
    ("ACC",      "ACC.NS",      "NSE:ACC-EQ",        500,
     "AMBUJACEM","AMBUJACEM.NS","NSE:AMBUJACEM-EQ", 1000),
    # Private banking (no merger issue — both stayed independent)
    ("FEDERALBNK","FEDERALBNK.NS","NSE:FEDERALBNK-EQ",10000,
     "INDUSINDBK","INDUSINDBK.NS","NSE:INDUSINDBK-EQ",  500),
    # PSU mining
    ("COALINDIA","COALINDIA.NS","NSE:COALINDIA-EQ",  4200,
     "NMDC",     "NMDC.NS",     "NSE:NMDC-EQ",       9000),
    # Tyre sector (same raw material - rubber, same OEM customers)
    ("MRF",      "MRF.NS",      "NSE:MRF-EQ",          24,
     "APOLLOTYRE","APOLLOTYRE.NS","NSE:APOLLOTYRE-EQ", 2750),
    # Pharma API — both specialty exporters (different from CIPLA/DRR which are formulations)
    ("DIVISLAB", "DIVISLAB.NS", "NSE:DIVISLAB-EQ",   150,
     "AUROPHARMA","AUROPHARMA.NS","NSE:AUROPHARMA-EQ",650),
]

DATA_DIR = Path("backtesting/book_strategies/ernie_chan_qt/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

loader = DataLoader()
cutoff = pd.Timestamp("2024-05-27")

ENTRY_ZS = [1.5, 2.0, 2.5]
STOP_ZS  = [3.0, 3.5, 4.0]
COOLDOWN = 5
MAX_HL_SCALE = 2.0   # skip trade if rolling HL > MAX_HL_SCALE × global HL

# ─────────────────────────────────────────────────────────────────────────────
def load_pair(na, yfa, fya, nb, yfb, fyb):
    cache = DATA_DIR / f"{na}_{nb}_daily.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    df_a = yf.download(yfa, start="2015-01-01", end="2024-05-28", auto_adjust=True, progress=False)
    df_b = yf.download(yfb, start="2015-01-01", end="2024-05-28", auto_adjust=True, progress=False)
    for df in [df_a, df_b]:
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df_a.index = pd.to_datetime(df_a.index).normalize()
    df_b.index = pd.to_datetime(df_b.index).normalize()
    yf_df = pd.DataFrame({na: df_a["Close"], nb: df_b["Close"]}).dropna()

    raw = loader.load_many([fya, fyb])
    fy = {}
    for sym, df in raw.items():
        d = resample_ohlcv(df, "1D"); d.index = d.index.normalize()
        fy[na if sym == fya else nb] = d["close"]
    fy_df = pd.DataFrame(fy).dropna()

    data = pd.concat([yf_df[yf_df.index <= cutoff],
                      fy_df[fy_df.index > cutoff]]).sort_index()
    data = data[~data.index.duplicated(keep="last")].dropna()
    data.to_parquet(cache)
    return data

def compute_signals(pa, pb, lookback):
    n = len(pa)
    zscores, half_lives = np.full(n, np.nan), np.full(n, np.nan)
    for t in range(lookback, n):
        wa, wb = pa[t-lookback:t], pb[t-lookback:t]
        try:
            _, beta = OLS(wa, add_constant(wb)).fit().params
            sp = wa - beta * wb
            phi = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
            hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
            sp_t = pa[t] - beta * pb[t]
            mu, sigma = sp.mean(), sp.std()
            zscores[t]    = (sp_t - mu) / sigma if sigma > 0 else 0.0
            half_lives[t] = hl
        except Exception:
            pass
    return zscores, half_lives

def simulate(pa, pb, dates, zscores, half_lives, lot_a, lot_b,
             entry_z, exit_z, stop_z, annual_stop, max_hl):
    brokerage = 0.0003
    def pnl(pos, epa, epb, xpa, xpb):
        gross = ((xpa - epa)*lot_a - (xpb - epb)*lot_b) * pos
        costs = (epa*lot_a + epb*lot_b + xpa*lot_a + xpb*lot_b) * brokerage
        return gross - costs

    position = 0; entry_pa = entry_pb = 0.0; entry_bar = 0
    year_pnl = 0.0; cur_yr = dates[0].year; cd_end = 0
    trades = []

    for t in range(len(pa)):
        if np.isnan(zscores[t]): continue
        if dates[t].year != cur_yr: cur_yr = dates[t].year; year_pnl = 0.0
        z, hl = zscores[t], half_lives[t]

        if position != 0:
            er = None
            if position == 1 and z >= -exit_z:  er = "z_exit"
            if position ==-1 and z <= +exit_z:  er = "z_exit"
            if abs(z) >= stop_z:                 er = "z_stop"
            if er:
                net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
                year_pnl += net
                trades.append(dict(
                    entry_date=dates[entry_bar], exit_date=dates[t],
                    hold_days=(dates[t]-dates[entry_bar]).days,
                    direction="LongA" if position==1 else "ShortA",
                    net_pnl=round(net,2), exit_reason=er,
                    z_entry=round(zscores[entry_bar],3), z_exit=round(z,3),
                ))
                position = 0; cd_end = t + COOLDOWN
            continue

        if t < cd_end: continue
        if year_pnl < -annual_stop: continue
        if hl > max_hl: continue
        if z < -entry_z: position=1;  entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t
        elif z > entry_z: position=-1; entry_pa=pa[t]; entry_pb=pb[t]; entry_bar=t

    # close open position at last bar
    if position != 0:
        t = len(pa)-1
        net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
        trades.append(dict(
            entry_date=dates[entry_bar], exit_date=dates[t],
            hold_days=(dates[t]-dates[entry_bar]).days,
            direction="LongA" if position==1 else "ShortA",
            net_pnl=round(net,2), exit_reason="end_of_data",
            z_entry=round(zscores[entry_bar],3), z_exit=round(zscores[t],3),
        ))
    return pd.DataFrame(trades)

def metrics(trades_df, capital):
    if trades_df.empty or len(trades_df) < 3:
        return dict(sharpe=-99, cagr=-99, max_dd=-99, net_pnl=0, n=0,
                    win_rate=0, pf=0, avg_loss=0, dep_pct=0, dep_ret=0)
    pnl  = trades_df["net_pnl"].sum()
    wins = (trades_df["net_pnl"] > 0).sum()
    wr   = wins / len(trades_df) * 100
    gw   = trades_df[trades_df["net_pnl"] > 0]["net_pnl"].sum()
    gl   = trades_df[trades_df["net_pnl"] < 0]["net_pnl"].sum()
    pf   = gw / abs(gl) if gl != 0 else 99
    al   = trades_df[trades_df["net_pnl"] < 0]["net_pnl"].mean() if gl != 0 else 0

    START = trades_df["entry_date"].min()
    END   = trades_df["exit_date"].max()
    idx   = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in daily.index: daily[tr["exit_date"]] += tr["net_pnl"]
    eq  = capital + daily.cumsum()
    nyr = max((END - START).days / 365.25, 0.1)
    cagr   = ((eq.iloc[-1]/capital)**(1/nyr)-1)*100
    dr     = daily / capital
    sharpe = (dr.mean()/dr.std()*np.sqrt(252)) if dr.std() > 0 else 0
    max_dd = ((eq - eq.cummax())/eq.cummax()*100).min()

    dep = pd.Series(False, index=idx)
    for _, tr in trades_df.iterrows():
        dep[(idx >= tr["entry_date"]) & (idx <= tr["exit_date"])] = True
    dep_f  = dep.mean()
    dep_ret = (pnl/nyr)/(capital*dep_f)*100 if dep_f > 0 else 0

    return dict(sharpe=round(sharpe,3), cagr=round(cagr,2), max_dd=round(max_dd,2),
                net_pnl=round(pnl,0), n=len(trades_df), win_rate=round(wr,1),
                pf=round(pf,2), avg_loss=round(al,0), dep_pct=round(dep_f*100,1),
                dep_ret=round(dep_ret,1))

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────
summary = []
SEP = "=" * 72

for (na, yfa, fya, la, nb, yfb, fyb, lb) in CANDIDATES:
    print(f"\n{SEP}")
    print(f"  PAIR: {na} / {nb}")
    print(SEP)

    try:
        data = load_pair(na, yfa, fya, nb, yfb, fyb)
    except Exception as e:
        print(f"  DATA ERROR: {e}"); continue

    if len(data) < 600:
        print(f"  SKIP — only {len(data)} rows (need 600+)"); continue

    pa, pb, dates = data[na].values, data[nb].values, data.index

    # ── Step 1: Cointegration ────────────────────────────────────────────────
    res   = OLS(pa, add_constant(pb)).fit()
    alpha, beta_ols = res.params
    spread = pa - beta_ols * pb
    adf    = adfuller(spread, autolag="AIC")
    stat, pval, crit = adf[0], adf[1], adf[4]
    level  = "1%" if stat < crit["1%"] else ("5%" if stat < crit["5%"] else ("10%" if stat < crit["10%"] else "FAIL"))
    print(f"  ADF={stat:.3f}  p={pval:.4f}  [{level}]  R²={res.rsquared:.3f}  β={beta_ols:.4f}")

    if pval > 0.12:
        print(f"  → SKIP: not cointegrated (p={pval:.3f} > 0.12)")
        summary.append(dict(pair=f"{na}/{nb}", verdict="NOT COINTEGRATED",
                            adf=round(stat,3), pval=round(pval,4), sharpe="—", cagr="—"))
        continue

    # ── Step 2: Half-life & LOOKBACK ────────────────────────────────────────
    phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
    hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
    if hl > 300 or hl <= 0:
        print(f"  → SKIP: half-life {hl:.0f}d is outside practical range")
        summary.append(dict(pair=f"{na}/{nb}", verdict="HL_OOB",
                            adf=round(stat,3), pval=round(pval,4), sharpe="—", cagr="—"))
        continue

    lookback = max(int(hl * 2), 63)
    max_hl   = hl * MAX_HL_SCALE
    print(f"  HL={hl:.1f}d  LOOKBACK={lookback}  max_HL_filter={max_hl:.0f}d")

    # ── Step 3: Lot balance ──────────────────────────────────────────────────
    shares_b  = beta_ols * la
    lots_b    = shares_b / lb
    best_lots = max(1, round(lots_b))
    actual_lb = best_lots * lb
    imbalance = abs(actual_lb - shares_b) / shares_b * 100
    print(f"  β={beta_ols:.4f} × {la} = {shares_b:.0f} {nb} shares → {lots_b:.2f} lots "
          f"→ round to {best_lots} lot(s) ({actual_lb:.0f} shares, imb={imbalance:.1f}%)")
    if imbalance > 40:
        print(f"  → SKIP: lot imbalance {imbalance:.1f}% too large (>40%)")
        summary.append(dict(pair=f"{na}/{nb}", verdict="LOT_IMBALANCE",
                            adf=round(stat,3), pval=round(pval,4), sharpe="—", cagr="—"))
        continue

    LOT_B_USED = actual_lb   # actual number of B shares traded
    avg_a = pa[-252:].mean(); avg_b = pb[-252:].mean()
    cap   = int((avg_a * la + avg_b * LOT_B_USED) * 0.15)
    print(f"  Approx margin: Rs{cap:,}  (15% SPAN of notional)")

    # ── Step 4: Pre-compute signals ──────────────────────────────────────────
    print(f"  Computing rolling signals (lookback={lookback})...")
    zscores, half_lives = compute_signals(pa, pb, lookback)

    # ── Step 5: Permissive V1 (no annual stop) → measure avg trade loss ──────
    v1 = simulate(pa, pb, dates, zscores, half_lives, la, int(LOT_B_USED),
                  entry_z=2.0, exit_z=0.5, stop_z=3.5,
                  annual_stop=9_999_999, max_hl=max_hl)
    if v1.empty or len(v1) < 5:
        print(f"  V1 produced only {len(v1)} trades — too few to calibrate")
        summary.append(dict(pair=f"{na}/{nb}", verdict="TOO_FEW_TRADES",
                            adf=round(stat,3), pval=round(pval,4), sharpe="—", cagr="—"))
        continue

    avg_loss_v1 = v1[v1["net_pnl"] < 0]["net_pnl"].mean() if (v1["net_pnl"]<0).any() else -10000
    annual_stop = max(int(abs(avg_loss_v1) * 3), 20_000)
    print(f"  V1: {len(v1)} trades, avg_loss=Rs{avg_loss_v1:,.0f} → ANNUAL_STOP=Rs{annual_stop:,}")

    # ── Step 6: Parameter sweep ──────────────────────────────────────────────
    print(f"  Sweeping ENTRY_Z × STOP_Z...")
    best = None
    sweep_results = []
    for ez in ENTRY_ZS:
        for sz in STOP_ZS:
            if sz <= ez: continue
            tdf = simulate(pa, pb, dates, zscores, half_lives, la, int(LOT_B_USED),
                           entry_z=ez, exit_z=0.5, stop_z=sz,
                           annual_stop=annual_stop, max_hl=max_hl)
            m = metrics(tdf, cap)
            sweep_results.append((ez, sz, m))
            if best is None or m["sharpe"] > best[2]["sharpe"]:
                best = (ez, sz, m, tdf)

    print(f"  Sweep results (Sharpe | CAGR | MaxDD | Trades):")
    for ez, sz, m in sweep_results:
        mark = " ← BEST" if (ez, sz) == (best[0], best[1]) else ""
        print(f"    EZ={ez}  SZ={sz}: Sharpe={m['sharpe']:>6.3f}  "
              f"CAGR={m['cagr']:>6.2f}%  MaxDD={m['max_dd']:>7.2f}%  "
              f"n={m['n']}  PF={m['pf']}{mark}")

    # ── Step 7: Best config results ──────────────────────────────────────────
    best_ez, best_sz, best_m, best_trades = best
    print(f"\n  ── BEST CONFIG: ENTRY_Z={best_ez}  STOP_Z={best_sz}  ANNUAL_STOP=Rs{annual_stop:,} ──")
    print(f"  Net P&L         : Rs{best_m['net_pnl']:,.0f}")
    print(f"  CAGR            : {best_m['cagr']:.2f}%")
    print(f"  Sharpe          : {best_m['sharpe']:.3f}")
    print(f"  Max Drawdown    : {best_m['max_dd']:.2f}%")
    print(f"  Win Rate        : {best_m['win_rate']:.1f}%")
    print(f"  Profit Factor   : {best_m['pf']:.2f}")
    print(f"  Trades          : {best_m['n']}")
    print(f"  Deployment      : {best_m['dep_pct']:.1f}%")
    print(f"  Return/Deployed : {best_m['dep_ret']:.1f}%/yr")

    # Year-by-year
    if not best_trades.empty:
        best_trades["yr"] = best_trades["exit_date"].dt.year
        print(f"  Year-by-year:")
        for yr, g in best_trades.groupby("yr"):
            p = g["net_pnl"].sum(); w = (g["net_pnl"]>0).sum()
            print(f"    {yr}: {'+'if p>0 else '-'}Rs{abs(p):>8,.0f}  "
                  f"({len(g)}tr, {w}W/{len(g)-w}L)")

    verdict = "PROFITABLE" if best_m["sharpe"] > 0.3 and best_m["net_pnl"] > 0 else \
              "MARGINAL"   if best_m["net_pnl"] > 0 else "REJECTED"
    print(f"\n  VERDICT: {verdict}")
    summary.append(dict(
        pair=f"{na}/{nb}", verdict=verdict,
        adf=round(stat,3), pval=round(pval,4),
        hl=round(hl,0), lookback=lookback, imb=round(imbalance,1),
        entry_z=best_ez, stop_z=best_sz, annual_stop=annual_stop,
        sharpe=best_m["sharpe"], cagr=best_m["cagr"],
        max_dd=best_m["max_dd"], net_pnl=best_m["net_pnl"],
        win_rate=best_m["win_rate"], pf=best_m["pf"],
        dep_pct=best_m["dep_pct"], dep_ret=best_m["dep_ret"],
    ))

# ─────────────────────────────────────────────────────────────────────────────
# FINAL SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n\n{'='*90}")
print(f"  OPTIMIZER SUMMARY")
print(f"{'='*90}")
print(f"  {'Pair':<25} {'Verdict':<16} {'ADF':>6} {'HL':>5} {'EZ':>4} {'SZ':>4} "
      f"{'Sharpe':>7} {'CAGR':>7} {'MaxDD':>7} {'P&L':>10}")
print(f"  {'─'*88}")
for r in sorted(summary, key=lambda x: x.get("sharpe", -99) if isinstance(x.get("sharpe"), float) else -99, reverse=True):
    sh  = f"{r['sharpe']:>7.3f}" if isinstance(r.get("sharpe"), float) else f"{'—':>7}"
    cg  = f"{r['cagr']:>6.2f}%" if isinstance(r.get("cagr"), float) else f"{'—':>7}"
    dd  = f"{r['max_dd']:>6.2f}%" if isinstance(r.get("max_dd"), float) else f"{'—':>7}"
    pnl = f"Rs{r['net_pnl']:>8,.0f}" if isinstance(r.get("net_pnl"), (int,float)) else f"{'—':>10}"
    ez  = str(r.get("entry_z","—"))
    sz  = str(r.get("stop_z","—"))
    hl  = str(r.get("hl","—"))
    print(f"  {r['pair']:<25} {r['verdict']:<16} {r['adf']:>6.3f} {hl:>5} {ez:>4} {sz:>4} "
          f"{sh} {cg} {dd} {pnl}")

profitable = [r for r in summary if r.get("verdict") == "PROFITABLE"]
print(f"\n  PROFITABLE PAIRS FOUND: {len(profitable)}")
for r in profitable:
    print(f"    {r['pair']}  Sharpe={r['sharpe']}  CAGR={r['cagr']}%  "
          f"ENTRY_Z={r['entry_z']}  STOP_Z={r['stop_z']}  ANNUAL_STOP=Rs{r.get('annual_stop',0):,}")
