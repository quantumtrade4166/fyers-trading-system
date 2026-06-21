"""
detailed_pair_breakdown.py
Detailed year-by-year breakdown for 4 candidate pairs.
Uses cached parquet files from pair_optimizer.py.
Applies full calibration per pair (HL → LOOKBACK, V1 → ANNUAL_STOP, sweep → best ENTRY_Z/STOP_Z).
Outputs: per-year P&L, return%, trades, win%, max-DD, deployment%, deployed-return.
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
COOLDOWN = 5

# ── 4 pairs to analyse ────────────────────────────────────────────────────────
# (label, parquet_file, col_A, lot_A, col_B, lot_B_unit, capital)
PAIRS = [
    # OMC refiner pair: borderline 10% ADF, R²=0.936, HL≈105d, lot imb 7.6%
    ("HINDPETRO/IOC",           "HINDPETRO_IOC_daily.parquet",
     "HINDPETRO", 2100, "IOC", 2500),

    # Bajaj group reversed: BAJAJFINSV (A, 1 lot=500) vs BAJFINANCE (B, 125/lot)
    # β≈2.12 → 1060 BAJFINANCE shares → 8 lots (1000 shares, ~5.7% imb)
    ("BAJAJFINSV/BAJFINANCE",   "BAJFINANCE_BAJAJFINSV_daily.parquet",
     "BAJAJFINSV", 500, "BAJFINANCE", 125),
]

ENTRY_ZS = [1.5, 2.0, 2.5]
STOP_ZS  = [3.0, 3.5, 4.0]

# ── Signal computation ────────────────────────────────────────────────────────
def compute_signals(pa, pb, lookback):
    n = len(pa)
    zscores    = np.full(n, np.nan)
    half_lives = np.full(n, np.nan)
    for t in range(lookback, n):
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
    return zscores, half_lives

# ── Simulation ────────────────────────────────────────────────────────────────
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
        if dates[t].year != cur_yr:
            cur_yr = dates[t].year
            year_pnl = 0.0
        z, hl = zscores[t], half_lives[t]

        if position != 0:
            # MTM P&L of open position (booked + unrealised vs annual cap)
            mtm = ((pa[t] - entry_pa)*lot_a - (pb[t] - entry_pb)*lot_b) * position
            er = None
            if position == 1 and z >= -exit_z:         er = "z_exit"
            if position ==-1 and z <= +exit_z:         er = "z_exit"
            if abs(z) >= stop_z:                        er = "z_stop"
            if (year_pnl + mtm) < -annual_stop:        er = "annual_stop"  # MTM annual cap
            if er:
                net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
                year_pnl += net
                trades.append(dict(
                    entry_date=dates[entry_bar], exit_date=dates[t],
                    hold_days=(dates[t]-dates[entry_bar]).days,
                    direction="LongA" if position==1 else "ShortA",
                    net_pnl=round(net, 2), exit_reason=er,
                    z_entry=round(zscores[entry_bar], 3), z_exit=round(z, 3),
                ))
                position = 0; cd_end = t + COOLDOWN
            continue

        if t < cd_end: continue
        if year_pnl < -annual_stop: continue
        if hl > max_hl: continue
        if z < -entry_z:
            position = 1;  entry_pa = pa[t]; entry_pb = pb[t]; entry_bar = t
        elif z > entry_z:
            position = -1; entry_pa = pa[t]; entry_pb = pb[t]; entry_bar = t

    if position != 0:
        t = len(pa) - 1
        net = pnl(position, entry_pa, entry_pb, pa[t], pb[t])
        trades.append(dict(
            entry_date=dates[entry_bar], exit_date=dates[t],
            hold_days=(dates[t]-dates[entry_bar]).days,
            direction="LongA" if position==1 else "ShortA",
            net_pnl=round(net, 2), exit_reason="end_of_data",
            z_entry=round(zscores[entry_bar], 3), z_exit=round(zscores[t], 3),
        ))
    return pd.DataFrame(trades)

def sharpe_and_dd(trades_df, capital):
    if trades_df.empty:
        return 0, 0, 0
    START = trades_df["entry_date"].min()
    END   = trades_df["exit_date"].max()
    idx   = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    eq     = capital + daily.cumsum()
    dr     = daily / capital
    sharpe = (dr.mean() / dr.std() * np.sqrt(252)) if dr.std() > 0 else 0
    max_dd = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    nyr    = max((END - START).days / 365.25, 0.1)
    cagr   = ((eq.iloc[-1] / capital) ** (1/nyr) - 1) * 100
    return round(sharpe, 3), round(max_dd, 2), round(cagr, 2)

# ── Year-by-year detailed metrics ─────────────────────────────────────────────
def year_breakdown(trades_df, capital, dates_idx):
    """Return per-year dict with P&L, return%, trades, win%, max_dd, dep%, dep_ret%."""
    if trades_df.empty:
        return {}

    # Build full business-day index
    full_idx = pd.date_range(dates_idx[0], dates_idx[-1], freq="B")
    daily    = pd.Series(0.0, index=full_idx)
    in_trade = pd.Series(False, index=full_idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
        in_trade[(full_idx >= tr["entry_date"]) & (full_idx <= tr["exit_date"])] = True

    equity = capital + daily.cumsum()

    result = {}
    for yr in range(trades_df["exit_date"].dt.year.min(),
                    trades_df["exit_date"].dt.year.max() + 1):
        yr_mask_exit  = trades_df["exit_date"].dt.year == yr
        yr_mask_entry = trades_df["entry_date"].dt.year == yr
        yr_trades     = trades_df[yr_mask_exit]

        # Also include trades entered this year but exited next (still open end of year)
        # For P&L we use exit year
        if yr_trades.empty:
            # check if there are trades open during this year
            yr_start = pd.Timestamp(f"{yr}-01-01")
            yr_end   = pd.Timestamp(f"{yr}-12-31")
            open_in_yr = trades_df[
                (trades_df["entry_date"] <= yr_end) &
                (trades_df["exit_date"] >= yr_start)
            ]
            if open_in_yr.empty:
                continue
            # some trades span into this year — show 0 P&L for year
            yr_pnl = 0.0; n_trades = 0; wins = 0
        else:
            yr_pnl   = yr_trades["net_pnl"].sum()
            n_trades = len(yr_trades)
            wins     = (yr_trades["net_pnl"] > 0).sum()

        # Max DD within this year (from equity curve)
        yr_eq_mask = (full_idx.year == yr)
        yr_eq = equity[yr_eq_mask]
        if len(yr_eq) > 1:
            yr_max_dd = ((yr_eq - yr_eq.cummax()) / yr_eq.cummax() * 100).min()
        else:
            yr_max_dd = 0.0

        # Deployment this year
        yr_bdays = full_idx[yr_eq_mask]
        yr_dep   = in_trade[yr_eq_mask].mean() * 100

        # Annual return %
        yr_ret_pct = yr_pnl / capital * 100

        # Deployed return
        dep_frac = yr_dep / 100
        yr_days  = len(yr_bdays)
        yr_dep_ret = (yr_pnl / (capital * dep_frac)) * 100 if dep_frac > 0 else 0

        result[yr] = dict(
            pnl=round(yr_pnl, 0),
            ret_pct=round(yr_ret_pct, 2),
            n_trades=n_trades,
            wins=wins,
            win_rate=round(wins/n_trades*100, 1) if n_trades > 0 else 0,
            max_dd=round(yr_max_dd, 2),
            dep_pct=round(yr_dep, 1),
            dep_ret=round(yr_dep_ret, 1),
        )
    return result

# ── MAIN ──────────────────────────────────────────────────────────────────────
BIG_SEP = "=" * 80

for (label, pfile, col_a, lot_a, col_b, lot_b_unit) in PAIRS:
    print(f"\n{BIG_SEP}")
    print(f"  {label}")
    print(BIG_SEP)

    data = pd.read_parquet(DATA_DIR / pfile).dropna()
    if col_a not in data.columns or col_b not in data.columns:
        print(f"  ERROR: columns not found. Available: {list(data.columns)}")
        continue

    pa    = data[col_a].values
    pb    = data[col_b].values
    dates = data.index
    n     = len(data)
    print(f"  Data: {dates[0].date()} → {dates[-1].date()}  ({n} rows)")

    # ── Step 1: Cointegration ────────────────────────────────────────────────
    res       = OLS(pa, add_constant(pb)).fit()
    alpha, beta_ols = res.params
    spread    = pa - beta_ols * pb
    adf       = adfuller(spread, autolag="AIC")
    stat, pval, crit = adf[0], adf[1], adf[4]
    level     = ("1%" if stat < crit["1%"] else
                 "5%" if stat < crit["5%"] else
                 "10%" if stat < crit["10%"] else "FAIL")
    print(f"  ADF={stat:.3f}  p={pval:.4f}  [{level}]  R²={res.rsquared:.3f}  β={beta_ols:.4f}")
    if pval > 0.12:
        print(f"  → NOT COINTEGRATED (p={pval:.3f} > 0.12). Skip.")
        continue

    # ── Step 2: Half-life & LOOKBACK ─────────────────────────────────────────
    phi = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
    hl  = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
    lookback = max(int(hl * 2), 63)
    max_hl   = hl * 2.0
    print(f"  Half-life={hl:.1f}d  LOOKBACK={lookback}  max_HL_filter={max_hl:.0f}d")

    # ── Step 3: Lot balance ───────────────────────────────────────────────────
    shares_b  = beta_ols * lot_a
    lots_b    = shares_b / lot_b_unit
    best_lots = max(1, round(lots_b))
    actual_lb = best_lots * lot_b_unit
    imb       = abs(actual_lb - shares_b) / shares_b * 100
    print(f"  β={beta_ols:.4f} × {lot_a} = {shares_b:.0f} {col_b} shares → {lots_b:.2f} lots "
          f"→ {best_lots} lot(s) = {actual_lb:.0f} shares  imbalance={imb:.1f}%")
    if imb > 40:
        print(f"  → SKIP: lot imbalance {imb:.1f}% > 40%")
        continue

    avg_a = pa[-252:].mean()
    avg_b = pb[-252:].mean()
    cap   = int((avg_a * lot_a + avg_b * actual_lb) * 0.15)
    print(f"  Capital (15% SPAN): Rs{cap:,}")

    # ── Step 4: Pre-compute signals ───────────────────────────────────────────
    print(f"  Computing signals (lookback={lookback})...", end=" ", flush=True)
    zscores, half_lives = compute_signals(pa, pb, lookback)
    print("done")

    # ── Step 5: Permissive V1 → ANNUAL_STOP ──────────────────────────────────
    v1 = simulate(pa, pb, dates, zscores, half_lives, lot_a, int(actual_lb),
                  entry_z=2.0, exit_z=0.5, stop_z=3.5,
                  annual_stop=9_999_999, max_hl=max_hl)
    avg_loss_v1  = (v1[v1["net_pnl"] < 0]["net_pnl"].mean()
                    if not v1.empty and (v1["net_pnl"] < 0).any() else -10000)
    annual_stop  = max(int(abs(avg_loss_v1) * 3), 20_000)
    print(f"  V1: {len(v1)} trades, avg_loss=Rs{avg_loss_v1:,.0f} → ANNUAL_STOP=Rs{annual_stop:,}")

    # ── Step 6: Parameter sweep ───────────────────────────────────────────────
    print(f"  Sweeping ENTRY_Z × STOP_Z...")
    best = None
    for ez in ENTRY_ZS:
        for sz in STOP_ZS:
            if sz <= ez: continue
            tdf = simulate(pa, pb, dates, zscores, half_lives, lot_a, int(actual_lb),
                           entry_z=ez, exit_z=0.5, stop_z=sz,
                           annual_stop=annual_stop, max_hl=max_hl)
            sh, _, _ = sharpe_and_dd(tdf, cap)
            if best is None or sh > best[2]:
                best = (ez, sz, sh, tdf)

    best_ez, best_sz, best_sh, best_trades = best
    best_sh, best_dd, best_cagr = sharpe_and_dd(best_trades, cap)
    print(f"  Best: ENTRY_Z={best_ez}  STOP_Z={best_sz}  Sharpe={best_sh:.3f}")

    # ── Step 7: Full metrics ──────────────────────────────────────────────────
    if best_trades.empty:
        print("  No trades — skip")
        continue

    bt = best_trades
    net_pnl  = bt["net_pnl"].sum()
    n_trades = len(bt)
    wins     = (bt["net_pnl"] > 0).sum()
    win_rate = wins / n_trades * 100
    gw = bt[bt["net_pnl"] > 0]["net_pnl"].sum()
    gl = bt[bt["net_pnl"] < 0]["net_pnl"].sum()
    pf = gw / abs(gl) if gl != 0 else 99
    avg_hold = bt["hold_days"].mean()

    START = bt["entry_date"].min()
    END   = bt["exit_date"].max()
    idx2  = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx2)
    for _, tr in bt.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    equity = cap + daily.cumsum()
    nyr    = (END - START).days / 365.25
    dep_s  = pd.Series(False, index=idx2)
    for _, tr in bt.iterrows():
        dep_s[(idx2 >= tr["entry_date"]) & (idx2 <= tr["exit_date"])] = True
    dep_pct = dep_s.mean() * 100
    dep_ret = (net_pnl / nyr) / (cap * dep_s.mean()) * 100 if dep_s.mean() > 0 else 0

    verdict = ("PROFITABLE" if best_sh > 0.3 and net_pnl > 0 else
               "MARGINAL"   if net_pnl > 0 else "REJECTED")

    SEP2 = "─" * 80
    print(f"\n  {SEP2}")
    print(f"  SUMMARY  [{verdict}]")
    print(f"  {SEP2}")
    print(f"  Parameters     : LOOKBACK={lookback}, ENTRY_Z={best_ez}, STOP_Z={best_sz}, "
          f"ANNUAL_STOP=Rs{annual_stop:,}")
    print(f"  Lot sizes      : {col_a}={lot_a} shares, {col_b}={int(actual_lb)} shares ({best_lots} lot)")
    print(f"  Capital        : Rs{cap:,}")
    print(f"  Period         : {START.date()} to {END.date()}  ({nyr:.1f} yr)")
    print(f"  Net P&L        : Rs{net_pnl:,.0f}")
    print(f"  CAGR           : {best_cagr:.2f}%")
    print(f"  Sharpe         : {best_sh:.3f}")
    print(f"  Max Drawdown   : {best_dd:.2f}%")
    print(f"  Win Rate       : {win_rate:.1f}%  ({wins}W / {n_trades-wins}L)")
    print(f"  Profit Factor  : {pf:.2f}")
    print(f"  Total Trades   : {n_trades}")
    print(f"  Avg Hold       : {avg_hold:.1f} days")
    print(f"  Deployment     : {dep_pct:.1f}% of days")
    print(f"  Return/Deployed: {dep_ret:.1f}%/yr")

    # ── Year-by-year table ────────────────────────────────────────────────────
    yb = year_breakdown(bt, cap, dates)

    print(f"\n  {'─'*80}")
    print(f"  YEAR-BY-YEAR BREAKDOWN  (Capital=Rs{cap:,})")
    print(f"  {'─'*80}")
    hdr = (f"  {'Year':<6} {'P&L':>10} {'Return%':>8} {'Trades':>7} "
           f"{'WinRate':>8} {'MaxDD%':>8} {'Deploy%':>8} {'DepRet%':>9}")
    print(hdr)
    print(f"  {'─'*80}")

    total_pnl = 0
    for yr in sorted(yb.keys()):
        r = yb[yr]
        sign   = "+" if r["pnl"] >= 0 else "-"
        colour = sign
        wr_str = f"{r['win_rate']:.0f}%" if r["n_trades"] > 0 else "—"
        trades_str = str(r["n_trades"]) if r["n_trades"] > 0 else "—"
        wins_str   = f"({r['wins']}W/{r['n_trades']-r['wins']}L)" if r["n_trades"] > 0 else ""
        print(f"  {yr:<6} {sign}Rs{abs(r['pnl']):>8,.0f}  {r['ret_pct']:>+7.2f}%  "
              f"{r['n_trades']:>5} {wins_str:<9}  {r['win_rate']:>5.0f}%  "
              f"{r['max_dd']:>+7.2f}%  {r['dep_pct']:>6.1f}%  {r['dep_ret']:>+8.1f}%")
        total_pnl += r["pnl"]

    print(f"  {'─'*80}")
    print(f"  {'TOTAL':<6} {'Rs'+str(f'{int(total_pnl):,}'):>10}  {total_pnl/cap*100:>+7.2f}%  "
          f"{n_trades:>5}  {' ':<9}  {win_rate:>5.0f}%  {best_dd:>+7.2f}%  "
          f"{dep_pct:>6.1f}%  {dep_ret:>+8.1f}%")
    print(f"  {'─'*80}")

    # ── Stop analysis ──────────────────────────────────────────────────────────
    stop_exits = (bt["exit_reason"] == "z_stop").sum()
    yr_totals  = bt.groupby(bt["exit_date"].dt.year)["net_pnl"].sum()
    fired_yrs  = [str(y) for y, p in yr_totals.items() if p < -annual_stop]
    avg_loss_t = (bt[bt["net_pnl"] < 0]["net_pnl"].mean()
                  if (bt["net_pnl"] < 0).any() else 0)
    print(f"\n  Stop analysis:")
    print(f"    Z-stop exits       : {stop_exits}/{n_trades}")
    print(f"    Annual stop fired  : {fired_yrs if fired_yrs else 'NEVER'}")
    print(f"    Avg loss per trade : Rs{avg_loss_t:,.0f}")

print(f"\n{BIG_SEP}")
print("  DONE")
print(BIG_SEP)
