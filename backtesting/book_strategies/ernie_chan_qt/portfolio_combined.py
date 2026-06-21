"""
portfolio_combined.py
Combined 3-pair portfolio: TCS/INFY + NTPC/POWERGRID + BAJAJFINSV/BAJFINANCE.

Columns explained:
  P&L           - net rupee profit/loss for the year
  ROI%          - P&L / THIS pair's own capital × 100
  Port ROI%     - P&L / TOTAL portfolio capital (Rs7.89L) × 100
  Max Cap Used  - highest Rs amount simultaneously in open trades (that year)
  Time Dep%     - % of trading days capital was in an open position
  MaxDD%        - peak-to-trough equity drawdown within that year
  Dep Return%   - annualised return while deployed: P&L / (capital × time_dep_fraction)
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

DATA_DIR = Path("backtesting/book_strategies/ernie_chan_qt/data")
BROK     = 0.0003
COOLDOWN = 5
EXIT_Z   = 0.5
cutoff   = pd.Timestamp("2024-05-27")

# ── Pair configs ──────────────────────────────────────────────────────────────
PAIRS = [
    dict(
        label="TCS/INFY",
        file="tcs_infy_daily_2015_2024.parquet",
        colA="TCS",       lotA=150,  fyers_A="NSE:TCS-EQ",
        colB="INFY",      lotB=600,  fyers_B="NSE:INFY-EQ",
        lookback=126, entry_z=2.0, stop_z=3.5, annual_stop=58_000, cap=200_000,
    ),
    dict(
        label="NTPC/POWERGRID",
        file="ntpc_powergrid_daily_2015_2024.parquet",
        colA="NTPC",      lotA=3250, fyers_A="NSE:NTPC-EQ",
        colB="POWERGRID", lotB=4200, fyers_B="NSE:POWERGRID-EQ",
        lookback=252, entry_z=2.0, stop_z=3.5, annual_stop=80_000, cap=300_000,
    ),
    dict(
        label="BAJAJFINSV/BAJFINANCE",
        file="BAJFINANCE_BAJAJFINSV_daily.parquet",
        colA="BAJAJFINSV", lotA=500,  fyers_A=None,
        colB="BAJFINANCE", lotB=1000, fyers_B=None,
        lookback=166, entry_z=2.5, stop_z=3.0, annual_stop=54_444, cap=289_535,
    ),
]

TOTAL_CAP = sum(p["cap"] for p in PAIRS)

# ── Data loading ──────────────────────────────────────────────────────────────
def load_or_stitch(cfg):
    fpath = DATA_DIR / cfg["file"]
    data  = pd.read_parquet(fpath).dropna()
    cA, cB = cfg["colA"], cfg["colB"]
    if cA not in data.columns:
        data.columns = [c.upper() for c in data.columns]
    if data.index[-1] >= pd.Timestamp("2025-01-01") or cfg["fyers_A"] is None:
        print(f"  [{cfg['label']}] {len(data)} rows "
              f"({data.index[0].date()} → {data.index[-1].date()})")
        return data[[cA, cB]]
    print(f"  [{cfg['label']}] stitching Fyers from {data.index[-1].date()}...")
    loader = DataLoader()
    raw    = loader.load_many([cfg["fyers_A"], cfg["fyers_B"]])
    fy = {}
    for sym, df in raw.items():
        d = resample_ohlcv(df, "1D"); d.index = d.index.normalize()
        fy[cA if sym == cfg["fyers_A"] else cB] = d["close"]
    fy_df = pd.DataFrame(fy).dropna()
    fy_df = fy_df[fy_df.index > cutoff]
    out = pd.concat([data[[cA, cB]], fy_df]).sort_index()
    out = out[~out.index.duplicated(keep="last")].dropna()
    print(f"         → {len(out)} rows after stitch ({out.index[-1].date()})")
    return out[[cA, cB]]

# ── Signals ───────────────────────────────────────────────────────────────────
def compute_signals(pa, pb, lookback):
    n = len(pa)
    zs = np.full(n, np.nan); hls = np.full(n, np.nan)
    for t in range(lookback, n):
        wa, wb = pa[t-lookback:t], pb[t-lookback:t]
        try:
            _, beta = OLS(wa, add_constant(wb)).fit().params
            sp = wa - beta*wb
            phi = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
            hl = -np.log(2)/np.log(1+phi) if phi < 0 else 999
            sp_t = pa[t] - beta*pb[t]
            mu, sig = sp.mean(), sp.std()
            zs[t] = (sp_t-mu)/sig if sig > 0 else 0.0; hls[t] = hl
        except: pass
    return zs, hls

# ── Backtest ──────────────────────────────────────────────────────────────────
def simulate(pa, pb, dates, zs, hls, cfg):
    lotA = cfg["lotA"]; lotB = cfg["lotB"]
    ez = cfg["entry_z"]; sz = cfg["stop_z"]
    ann_stop = cfg["annual_stop"]; max_hl = cfg["lookback"]

    def pnl_calc(pos, epa, epb, xpa, xpb):
        gross = ((xpa-epa)*lotA - (xpb-epb)*lotB) * pos
        costs = (epa*lotA+epb*lotB+xpa*lotA+xpb*lotB)*BROK
        return gross - costs

    pos = 0; e_pa = e_pb = 0.0; e_bar = 0
    yr_pnl = 0.0; cur_yr = dates[0].year; cd_end = 0
    trades = []
    for t in range(len(pa)):
        if np.isnan(zs[t]): continue
        if dates[t].year != cur_yr: cur_yr = dates[t].year; yr_pnl = 0.0
        z, hl = zs[t], hls[t]
        if pos != 0:
            mtm = ((pa[t]-e_pa)*lotA - (pb[t]-e_pb)*lotB)*pos
            er  = None
            if pos==1  and z >= -EXIT_Z:  er = "z_exit"
            if pos==-1 and z <= +EXIT_Z:  er = "z_exit"
            if abs(z) >= sz:               er = "z_stop"
            if (yr_pnl+mtm) < -ann_stop:  er = "annual_stop"
            if er:
                net = pnl_calc(pos, e_pa, e_pb, pa[t], pb[t])
                yr_pnl += net
                trades.append(dict(
                    entry_date=dates[e_bar], exit_date=dates[t],
                    net_pnl=round(net,2), exit_reason=er,
                    hold_days=(dates[t]-dates[e_bar]).days,
                ))
                pos = 0; cd_end = t+COOLDOWN
            continue
        if t < cd_end or yr_pnl < -ann_stop or hl > max_hl: continue
        if z < -ez:  pos=1;  e_pa=pa[t]; e_pb=pb[t]; e_bar=t
        elif z > ez: pos=-1; e_pa=pa[t]; e_pb=pb[t]; e_bar=t
    if pos != 0:
        t = len(pa)-1
        net = pnl_calc(pos, e_pa, e_pb, pa[t], pb[t])
        trades.append(dict(entry_date=dates[e_bar], exit_date=dates[t],
                           net_pnl=round(net,2), exit_reason="end_of_data",
                           hold_days=(dates[t]-dates[e_bar]).days))
    return pd.DataFrame(trades)

# ── Build daily series ────────────────────────────────────────────────────────
def build_daily(trades, full_idx, cap):
    daily    = pd.Series(0.0, index=full_idx)
    in_trade = pd.Series(False, index=full_idx)
    for _, tr in trades.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
        mask = (full_idx >= tr["entry_date"]) & (full_idx <= tr["exit_date"])
        in_trade[mask] = True
    equity = cap + daily.cumsum()
    return daily, in_trade, equity

# ── Year stats for ONE pair ───────────────────────────────────────────────────
def pair_year_stats(daily, in_trade, equity, cap, trades, full_idx):
    result = {}
    for yr in sorted(set(full_idx.year)):
        mask     = full_idx.year == yr
        yr_pnl   = daily[mask].sum()
        yr_eq    = equity[mask]
        dep_frac = in_trade[mask].mean()
        dep_pct  = dep_frac * 100
        yr_tr    = trades[trades["exit_date"].dt.year==yr] if not trades.empty else pd.DataFrame()
        n_tr     = len(yr_tr)
        wins     = (yr_tr["net_pnl"]>0).sum() if n_tr>0 else 0

        max_dd  = ((yr_eq - yr_eq.cummax())/yr_eq.cummax()*100).min() if len(yr_eq)>1 else 0.0
        roi_own = yr_pnl / cap * 100
        roi_tot = yr_pnl / TOTAL_CAP * 100
        dep_ret = (yr_pnl / (cap * dep_frac)) * 100 if dep_frac > 0 else 0.0
        # max capital used this year = cap if any day in trade, else 0 (single pair)
        max_cap = cap if in_trade[mask].any() else 0

        result[yr] = dict(
            pnl=round(yr_pnl,0), roi_own=round(roi_own,2), roi_tot=round(roi_tot,2),
            max_cap=int(max_cap), dep_pct=round(dep_pct,1),
            max_dd=round(max_dd,2), dep_ret=round(dep_ret,1),
            n_trades=n_tr, wins=wins,
            win_rate=round(wins/n_tr*100,1) if n_tr>0 else 0.0,
        )
    return result

# ── Year stats for COMBINED portfolio ────────────────────────────────────────
def portfolio_year_stats(pair_daily_map, pair_in_trade_map, pair_cap_map,
                          combined_daily, combined_equity, full_idx):
    """
    pair_cap_map  : {label: cap}
    pair_in_trade_map : {label: bool Series}
    For max_cap_used: on each day, sum capitals of pairs currently in trade.
    """
    result = {}
    labels = list(pair_cap_map.keys())

    # daily capital deployed (Rs) = sum of cap[i] for each pair in trade that day
    daily_cap_deployed = sum(
        pair_in_trade_map[lb].astype(float) * pair_cap_map[lb]
        for lb in labels
    )

    for yr in sorted(set(full_idx.year)):
        mask        = full_idx.year == yr
        yr_pnl      = combined_daily[mask].sum()
        yr_eq       = combined_equity[mask]
        yr_dep_cap  = daily_cap_deployed[mask]

        max_cap_used = int(yr_dep_cap.max())        # highest Rs simultaneously deployed
        avg_cap_used = yr_dep_cap.mean()             # average daily deployment
        time_dep_pct = (yr_dep_cap > 0).mean() * 100  # % of days ANYTHING was in trade

        max_dd   = ((yr_eq-yr_eq.cummax())/yr_eq.cummax()*100).min() if len(yr_eq)>1 else 0.0
        roi_pct  = yr_pnl / TOTAL_CAP * 100
        # deployed return: P&L relative to avg capital deployed
        dep_ret  = (yr_pnl / avg_cap_used) * 100 if avg_cap_used > 0 else 0.0

        n_tr = sum(
            len(pair_daily_map[lb])  # placeholder — count separately
            for lb in labels
        )

        result[yr] = dict(
            pnl=round(yr_pnl,0), roi_pct=round(roi_pct,2),
            max_cap_used=max_cap_used, time_dep_pct=round(time_dep_pct,1),
            max_dd=round(max_dd,2), dep_ret=round(dep_ret,1),
        )
    return result

# ═══════════════════════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════════════════════
print("\nLoading & computing signals...")
pair_data = {}
for cfg in PAIRS:
    data   = load_or_stitch(cfg)
    cA, cB = cfg["colA"], cfg["colB"]
    pa, pb  = data[cA].values, data[cB].values
    dates   = data.index
    print(f"  Signals {cfg['label']}...", end=" ", flush=True)
    zs, hls = compute_signals(pa, pb, cfg["lookback"])
    trades  = simulate(pa, pb, dates, zs, hls, cfg)
    print(f"done  ({len(trades)} trades)")
    pair_data[cfg["label"]] = dict(pa=pa, pb=pb, dates=dates, trades=trades,
                                   cap=cfg["cap"])

# Common index
all_starts = [pair_data[lb]["dates"][0] for lb in pair_data]
all_ends   = [pair_data[lb]["dates"][-1] for lb in pair_data]
full_idx   = pd.date_range(max(all_starts), min(all_ends), freq="B")
print(f"\nCommon range: {full_idx[0].date()} → {full_idx[-1].date()} "
      f"({len(full_idx)} trading days)")

# Build daily series
pair_daily_s    = {}
pair_in_trade_s = {}
pair_equity_s   = {}
for lb, d in pair_data.items():
    dy, it, eq = build_daily(d["trades"], full_idx, d["cap"])
    pair_daily_s[lb]    = dy
    pair_in_trade_s[lb] = it
    pair_equity_s[lb]   = eq

combined_daily  = sum(pair_daily_s.values())
combined_equity = TOTAL_CAP + combined_daily.cumsum()

pair_cap_map = {lb: pair_data[lb]["cap"] for lb in pair_data}

# Year stats
pair_ystats = {}
for lb in pair_data:
    trd = pair_data[lb]["trades"]
    # clip to common range
    if not trd.empty:
        trd = trd[(trd["entry_date"] >= full_idx[0]) & (trd["exit_date"] <= full_idx[-1])]
    pair_ystats[lb] = pair_year_stats(
        pair_daily_s[lb], pair_in_trade_s[lb], pair_equity_s[lb],
        pair_data[lb]["cap"], trd, full_idx
    )

combined_ystats = portfolio_year_stats(
    pair_daily_s, pair_in_trade_s, pair_cap_map,
    combined_daily, combined_equity, full_idx
)

# ── Count combined trades per year ────────────────────────────────────────────
combined_trade_count = {}
for lb in pair_data:
    trd = pair_data[lb]["trades"]
    if trd.empty: continue
    trd = trd[(trd["entry_date"] >= full_idx[0]) & (trd["exit_date"] <= full_idx[-1])]
    for yr, grp in trd.groupby(trd["exit_date"].dt.year):
        combined_trade_count[yr] = combined_trade_count.get(yr, 0) + len(grp)

# ═══════════════════════════════════════════════════════════════════════════════
#  PRINT
# ═══════════════════════════════════════════════════════════════════════════════
SEP  = "=" * 120
SEP2 = "─" * 120
labels = list(pair_data.keys())

print(f"\n\n{SEP}")
print(f"  COLUMN GUIDE")
print(SEP)
print(f"  P&L         = Net profit/loss in rupees for the year")
print(f"  Own ROI%    = P&L / this pair's own capital × 100")
print(f"  Port ROI%   = P&L / TOTAL portfolio capital (Rs{TOTAL_CAP:,}) × 100")
print(f"  Max Cap(Rs) = Highest rupee amount in open trades at any single day this year")
print(f"  Time Dep%   = % of trading days the pair had an open position")
print(f"  MaxDD%      = Worst peak-to-trough equity drop within the year")
print(f"  Dep Ret%    = Return earned while deployed: P&L / (capital × time_dep_frac)")

# ── Individual pair tables ────────────────────────────────────────────────────
for lb in labels:
    cap = pair_data[lb]["cap"]
    ys  = pair_ystats[lb]
    trd = pair_data[lb]["trades"]
    if not trd.empty:
        trd = trd[(trd["entry_date"]>=full_idx[0])&(trd["exit_date"]<=full_idx[-1])]

    print(f"\n{SEP}")
    print(f"  {lb}   [Capital = Rs{cap:,}  |  Portfolio weight = {cap/TOTAL_CAP*100:.1f}%]")
    print(SEP)
    hdr = (f"  {'Year':<6} {'P&L':>11} {'Own ROI%':>9} {'Port ROI%':>10} "
           f"{'Max Cap(Rs)':>12} {'Time Dep%':>10} {'MaxDD%':>8} {'Dep Ret%':>9}")
    print(hdr)
    print(f"  {SEP2[:90]}")

    tot_pnl = 0
    for yr in sorted(ys.keys()):
        r = ys[yr]
        tot_pnl += r["pnl"]
        s = "+" if r["pnl"] >= 0 else "-"
        mc = f"Rs{r['max_cap']:,}" if r['max_cap'] > 0 else "—"
        print(f"  {yr:<6} {s}Rs{abs(r['pnl']):>8,.0f}  {r['roi_own']:>+8.2f}%  "
              f"{r['roi_tot']:>+9.2f}%  {mc:>12}  {r['dep_pct']:>8.1f}%  "
              f"{r['max_dd']:>+7.2f}%  {r['dep_ret']:>+8.1f}%")

    n_all  = len(trd) if not trd.empty else 0
    wr_all = (trd["net_pnl"]>0).sum()/n_all*100 if n_all>0 else 0
    dep_fr = pair_in_trade_s[lb].mean()
    dep_ret_all = ((tot_pnl / ((pair_equity_s[lb].index[-1]-pair_equity_s[lb].index[0]).days/365.25))
                   / (cap * dep_fr) * 100) if dep_fr > 0 else 0
    nyr_p  = (full_idx[-1]-full_idx[0]).days/365.25
    cagr_p = ((pair_equity_s[lb].iloc[-1]/cap)**(1/nyr_p)-1)*100
    print(f"  {SEP2[:90]}")
    print(f"  {'TOTAL':<6} {'Rs'+str(f'{int(tot_pnl):,}'):>11}  "
          f"  {tot_pnl/cap*100:>+8.2f}%  {tot_pnl/TOTAL_CAP*100:>+9.2f}%  "
          f"{'Rs'+str(f'{cap:,}'):>12}  {dep_fr*100:>8.1f}%  "
          f"{'':>8}  {dep_ret_all:>+8.1f}%")
    print(f"  CAGR={cagr_p:+.2f}%   Trades={n_all}   WinRate={wr_all:.0f}%")

# ── Combined portfolio table ──────────────────────────────────────────────────
nyr = (full_idx[-1]-full_idx[0]).days/365.25
cagr_c = ((combined_equity.iloc[-1]/TOTAL_CAP)**(1/nyr)-1)*100
dr_c   = combined_daily/TOTAL_CAP
sharpe = dr_c.mean()/dr_c.std()*np.sqrt(252) if dr_c.std()>0 else 0
mdd_c  = ((combined_equity-combined_equity.cummax())/combined_equity.cummax()*100).min()
total_pnl = combined_daily.sum()

daily_cap_deployed = sum(
    pair_in_trade_s[lb].astype(float)*pair_cap_map[lb] for lb in labels
)

print(f"\n{SEP}")
print(f"  COMBINED PORTFOLIO  [Total Capital = Rs{TOTAL_CAP:,}]")
print(SEP)
print(f"  {'Year':<6} {'Combined P&L':>13} {'Port ROI%':>10} {'Max Cap Used':>13} "
      f"{'Time Dep%':>10} {'MaxDD%':>8} {'Dep Ret%':>9} {'Trades':>7}")
print(f"  {SEP2[:88]}")

tot_c = 0
for yr in sorted(combined_ystats.keys()):
    r  = combined_ystats[yr]
    n  = combined_trade_count.get(yr, 0)
    tot_c += r["pnl"]
    s  = "+" if r["pnl"] >= 0 else "-"
    mc = f"Rs{r['max_cap_used']:,}"
    print(f"  {yr:<6} {s}Rs{abs(r['pnl']):>10,.0f}  {r['roi_pct']:>+9.2f}%  "
          f"{mc:>13}  {r['time_dep_pct']:>9.1f}%  {r['max_dd']:>+7.2f}%  "
          f"{r['dep_ret']:>+8.1f}%  {n:>6}")

avg_cap_total = daily_cap_deployed.mean()
time_dep_tot  = (daily_cap_deployed > 0).mean()*100
dep_ret_total = (total_pnl / nyr / avg_cap_total * 100) if avg_cap_total > 0 else 0
print(f"  {SEP2[:88]}")
print(f"  {'TOTAL':<6} {'Rs'+str(f'{int(total_pnl):,}'):>13}  {total_pnl/TOTAL_CAP*100:>+9.2f}%  "
      f"{'Rs'+str(f'{int(daily_cap_deployed.max()):,}'):>13}  {time_dep_tot:>9.1f}%  "
      f"{mdd_c:>+7.2f}%  {dep_ret_total:>+8.1f}%  "
      f"{sum(combined_trade_count.values()):>6}")

# ── Final summary ─────────────────────────────────────────────────────────────
print(f"\n{SEP}")
print(f"  PORTFOLIO SUMMARY")
print(SEP)
print(f"  Period  : {full_idx[0].date()} → {full_idx[-1].date()}  ({nyr:.1f} years)")
print(f"  Capital : TCS/INFY Rs{pair_data['TCS/INFY']['cap']:,} + "
      f"NTPC/PG Rs{pair_data['NTPC/POWERGRID']['cap']:,} + "
      f"BAJAJ Rs{pair_data['BAJAJFINSV/BAJFINANCE']['cap']:,} = Rs{TOTAL_CAP:,}")
print()
print(f"  {'Metric':<28} {'TCS/INFY':>12} {'NTPC/PG':>12} {'BAJAJ/BAJ':>12} {'COMBINED':>12}")
print(f"  {'─'*70}")

ind = {}
for lb in labels:
    cap = pair_data[lb]["cap"]
    eq  = pair_equity_s[lb]
    dy  = pair_daily_s[lb]
    trd = pair_data[lb]["trades"]
    if not trd.empty:
        trd = trd[(trd["entry_date"]>=full_idx[0])&(trd["exit_date"]<=full_idx[-1])]
    tot = dy.sum()
    cg  = ((eq.iloc[-1]/cap)**(1/nyr)-1)*100
    dr  = dy/cap
    sh  = dr.mean()/dr.std()*np.sqrt(252) if dr.std()>0 else 0
    md  = ((eq-eq.cummax())/eq.cummax()*100).min()
    dp  = pair_in_trade_s[lb].mean()*100
    dp_ret = (tot/nyr)/(cap*dp/100)*100 if dp>0 else 0
    n   = len(trd); wr = (trd["net_pnl"]>0).sum()/n*100 if n>0 else 0
    ind[lb] = dict(pnl=tot, cagr=cg, sharpe=sh, mdd=md, dep=dp, dep_ret=dp_ret, n=n, wr=wr)

rows = [
    ("Net P&L",         lambda v,c: f"Rs{int(v['pnl']):,}",   f"Rs{int(total_pnl):,}"),
    ("CAGR %",          lambda v,c: f"{v['cagr']:+.2f}%",      f"{cagr_c:+.2f}%"),
    ("Sharpe",          lambda v,c: f"{v['sharpe']:.3f}",      f"{sharpe:.3f}"),
    ("Max DD %",        lambda v,c: f"{v['mdd']:.2f}%",        f"{mdd_c:.2f}%"),
    ("Time Deployed %", lambda v,c: f"{v['dep']:.1f}%",        f"{time_dep_tot:.1f}%"),
    ("Dep Return %/yr", lambda v,c: f"{v['dep_ret']:+.1f}%",   f"{dep_ret_total:+.1f}%"),
    ("Trades",          lambda v,c: str(v['n']),                str(sum(combined_trade_count.values()))),
    ("Win Rate",        lambda v,c: f"{v['wr']:.1f}%",         "—"),
]

for name, ind_fn, comb_val in rows:
    vals = [ind_fn(ind[lb], pair_data[lb]["cap"]) for lb in labels]
    print(f"  {name:<28} {vals[0]:>12} {vals[1]:>12} {vals[2]:>12} {comb_val:>12}")

print(f"\n{SEP}")
print("  DONE")
print(SEP)
