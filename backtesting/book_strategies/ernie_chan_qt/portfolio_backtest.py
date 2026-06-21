"""
portfolio_backtest.py  v2
Combined 11-pair portfolio: 3 existing + 8 new.

NTPC/POWERGRID and BAJAJFINSV/BAJFINANCE are RECALIBRATED at their new lot sizes:
  V1 (permissive) → derive annual_stop → sweep entry_z/stop_z → pick best Sharpe.

All pairs: lot-balance sweep n=1..10, SPAN=15% of notional.

Year-by-year columns:
  P&L | MaxCapUsed | Ret/MaxCap | MaxDD/MaxCap | Dep%
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
import yfinance as yf

STOCK_DIR   = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/data/stocks"
DATA_DIR    = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/data"
RESULTS_DIR = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

COOLDOWN   = 5
EXIT_Z     = 0.5
SPAN_RATE  = 0.15
MAX_LOT_N  = 10
BROK       = 0.0003
ENTRY_ZS   = [1.5, 2.0, 2.5]
STOP_ZS    = [3.0, 3.5, 4.0]

# ─────────────────────────────────────────────────────────────────────────────
# PAIRS: (label, symA, yf_A, lotA_unit, symB, yf_B, lotB_unit,
#         lookback, entry_z, stop_z, annual_stop)
#
# None for the last 4 params = RECALIBRATE via V1→sweep at current lot sizes.
# Fixed values = use pre-calibrated params (batch_backtest.py or prior sessions).
# ─────────────────────────────────────────────────────────────────────────────
PAIRS = [
    # ── Existing 2 (NTPC/POWERGRID removed — Sharpe 0.056, structural break) ──
    # TCS/INFY: calibrated in prior sessions — KEEP fixed params
    ("TCS/INFY",
     "TCS",        "TCS.NS",        150,
     "INFY",       "INFY.NS",       300,
     126, 2.0, 3.5, 58_000),

    # BAJAJFINSV/BAJFINANCE: new lots (4×500 / 33×125) — RECALIBRATE
    ("BAJAJFINSV/BAJFINANCE",
     "BAJAJFINSV", "BAJAJFINSV.NS", 500,
     "BAJFINANCE", "BAJFINANCE.NS", 125,
     None, None, None, None),

    # ── New 8 (calibrated in batch_backtest.py at their optimal lot sizes) ───
    ("HDFCBANK/KOTAKBANK",
     "HDFCBANK",   "HDFCBANK.NS",   550,
     "KOTAKBANK",  "KOTAKBANK.NS",  400,
     113, 2.0, 4.0, 393_340),

    ("HINDUNILVR/DABUR",
     "HINDUNILVR", "HINDUNILVR.NS", 300,
     "DABUR",      "DABUR.NS",     1250,
     67,  1.5, 4.0, 508_632),

    ("OBEROIRLTY/BRIGADE",
     "OBEROIRLTY", "OBEROIRLTY.NS", 800,
     "BRIGADE",    "BRIGADE.NS",   1000,
     92,  1.5, 3.5, 1_137_471),

    ("TATAPOWER/JSWENERGY",
     "TATAPOWER",  "TATAPOWER.NS", 4950,
     "JSWENERGY",  "JSWENERGY.NS", 2000,
     76,  2.0, 3.0, 1_211_814),

    ("TECHM/COFORGE",
     "TECHM",      "TECHM.NS",      600,
     "COFORGE",    "COFORGE.NS",    200,
     140, 1.5, 3.5, 501_534),

    ("EICHERMOT/TVSMOTORS",
     "EICHERMOT",  "EICHERMOT.NS",  200,
     "TVSMOTORS",  "TVSMOTOR.NS",   350,
     129, 1.5, 4.0, 554_023),

    ("HDFCLIFE/ICICIPRULI",
     "HDFCLIFE",   "HDFCLIFE.NS",  1100,
     "ICICIPRULI", "ICICIPRULI.NS",1500,
     87,  2.0, 4.0, 223_222),

    ("SRF/DEEPAKNTR",
     "SRF",        "SRF.NS",        250,
     "DEEPAKNTR",  "DEEPAKNTR.NS",  350,
     151, 2.0, 4.0, 1_052_772),
]

LEGACY_FILES = {
    "TCS/INFY": (
        DATA_DIR / "tcs_infy_daily_2015_2024_ext.parquet", "TCS", "INFY"),
    "NTPC/POWERGRID": (
        DATA_DIR / "ntpc_powergrid_daily_2015_2024_ext.parquet", "NTPC", "POWERGRID"),
    "BAJAJFINSV/BAJFINANCE": (
        DATA_DIR / "BAJFINANCE_BAJAJFINSV_daily.parquet", "BAJAJFINSV", "BAJFINANCE"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────

def load_stock(sym, yf_ticker):
    for suffix in ("_ext", "_yf"):
        p = STOCK_DIR / f"{sym}{suffix}.parquet"
        if p.exists():
            raw = pd.read_parquet(p)
            s   = raw.iloc[:, 0] if isinstance(raw, pd.DataFrame) else raw
            s.index = pd.to_datetime(s.index)
            return s.sort_index().dropna()
    raw = yf.download(yf_ticker, start="2015-01-01", end="2024-05-28",
                      progress=False, auto_adjust=True)
    if raw.empty:
        raise ValueError(f"No data for {yf_ticker}")
    s = raw["Close"].squeeze().sort_index().dropna()
    s.to_frame(name=sym).to_parquet(STOCK_DIR / f"{sym}_yf.parquet")
    return s


def load_pair(label, symA, yf_A, symB, yf_B):
    if label in LEGACY_FILES:
        p, colA, colB = LEGACY_FILES[label]
        if p.exists():
            df = pd.read_parquet(p).dropna()
            df.columns = [c.upper() for c in df.columns]
            sA = df[colA] if colA in df.columns else df.iloc[:, 0]
            sB = df[colB] if colB in df.columns else df.iloc[:, 1]
            sA.index = pd.to_datetime(sA.index)
            sB.index = pd.to_datetime(sB.index)
            return sA.sort_index().dropna(), sB.sort_index().dropna()
    return load_stock(symA, yf_A), load_stock(symB, yf_B)


# ─────────────────────────────────────────────────────────────────────────────
# Lot balance optimiser
# ─────────────────────────────────────────────────────────────────────────────

def find_optimal_lots(beta, lot_a, lot_b, max_n=MAX_LOT_N):
    best = None
    for n in range(1, max_n + 1):
        ideal_b  = beta * lot_a * n
        n_b      = max(1, round(ideal_b / lot_b))
        actual_b = n_b * lot_b
        imb      = abs(actual_b - ideal_b) / ideal_b * 100
        if best is None or imb < best[2]:
            best = (n, n_b, imb)
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Signal computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_signals(pa, pb, lookback):
    n          = len(pa)
    zscores    = np.full(n, np.nan)
    half_lives = np.full(n, np.nan)
    for t in range(lookback, n):
        wa, wb = pa[t-lookback:t], pb[t-lookback:t]
        try:
            _, beta = OLS(wa, add_constant(wb)).fit().params
            sp      = wa - beta * wb
            phi     = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
            hl      = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
            sp_t    = pa[t] - beta * pb[t]
            mu, sig = sp.mean(), sp.std()
            zscores[t]    = (sp_t - mu) / sig if sig > 0 else 0.0
            half_lives[t] = hl
        except Exception:
            pass
    return zscores, half_lives


# ─────────────────────────────────────────────────────────────────────────────
# Simulator
# ─────────────────────────────────────────────────────────────────────────────

def simulate(pa, pb, dates, zscores, half_lives,
             n_A_sh, n_B_sh, entry_z, stop_z, annual_stop, max_hl):

    def pnl_trade(pos, epa, epb, xpa, xpb):
        gross = ((xpa - epa) * n_A_sh - (xpb - epb) * n_B_sh) * pos
        cost  = (epa*n_A_sh + epb*n_B_sh + xpa*n_A_sh + xpb*n_B_sh) * BROK
        return gross - cost

    pos = 0; epa = epb = 0.0; ebar = 0
    yrpnl = 0.0; curyr = dates[0].year; cdend = 0
    trades = []

    for t in range(len(pa)):
        if np.isnan(zscores[t]):
            continue
        if dates[t].year != curyr:
            curyr = dates[t].year; yrpnl = 0.0
        z, hl = zscores[t], half_lives[t]

        if pos != 0:
            mtm = ((pa[t] - epa) * n_A_sh - (pb[t] - epb) * n_B_sh) * pos
            er  = None
            if pos ==  1 and z >= -EXIT_Z:    er = "z_exit"
            if pos == -1 and z <=  EXIT_Z:    er = "z_exit"
            if abs(z) >= stop_z:              er = "z_stop"
            if (yrpnl + mtm) < -annual_stop:  er = "annual_stop"
            if er:
                net    = pnl_trade(pos, epa, epb, pa[t], pb[t])
                yrpnl += net
                trades.append(dict(
                    entry_date=dates[ebar], exit_date=dates[t],
                    hold_days=(dates[t] - dates[ebar]).days,
                    direction="LongA" if pos == 1 else "ShortA",
                    net_pnl=round(net, 2), exit_reason=er,
                ))
                pos = 0; cdend = t + COOLDOWN
            continue

        if t < cdend or yrpnl < -annual_stop or hl > max_hl:
            continue
        if z < -entry_z:
            pos =  1; epa = pa[t]; epb = pb[t]; ebar = t
        elif z > entry_z:
            pos = -1; epa = pa[t]; epb = pb[t]; ebar = t

    if pos != 0:
        t = len(pa) - 1
        net = pnl_trade(pos, epa, epb, pa[t], pb[t])
        trades.append(dict(
            entry_date=dates[ebar], exit_date=dates[t],
            hold_days=(dates[t] - dates[ebar]).days,
            direction="LongA" if pos == 1 else "ShortA",
            net_pnl=round(net, 2), exit_reason="end_of_data",
        ))
    return pd.DataFrame(trades)


def sharpe_from_trades(trades_df, cap):
    if trades_df.empty:
        return 0.0, 0.0, 0.0
    START = trades_df["entry_date"].min()
    END   = trades_df["exit_date"].max()
    idx   = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    eq     = cap + daily.cumsum()
    dr     = daily / cap
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0.0
    maxdd  = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    nyr    = max((END - START).days / 365.25, 0.1)
    cagr   = ((max(eq.iloc[-1], 0.01) / cap) ** (1 / nyr) - 1) * 100
    return round(sharpe, 3), round(maxdd, 2), round(cagr, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Daily series builders
# ─────────────────────────────────────────────────────────────────────────────

def build_daily_pnl(trades_df, idx):
    daily = pd.Series(0.0, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    return daily


def build_daily_cap(trades_df, sA, sB, n_A_sh, n_B_sh):
    cap = pd.Series(0.0, index=sA.index)
    for _, tr in trades_df.iterrows():
        mask     = (sA.index >= tr["entry_date"]) & (sA.index <= tr["exit_date"])
        notional = SPAN_RATE * (n_A_sh * sA[mask] + n_B_sh * sB[mask])
        cap.loc[mask] = np.maximum(cap.loc[mask].values, notional.values)
    return cap


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SEP  = "=" * 82
    SEP2 = "─" * 82

    print(SEP)
    print(f"  PORTFOLIO BACKTEST v2 — {len(PAIRS)} pairs")
    print(f"  NTPC/POWERGRID and BAJAJFINSV/BAJFINANCE: RECALIBRATED at new lot sizes")
    print(f"  Lot-balance sweep n=1..{MAX_LOT_N}  SPAN={SPAN_RATE*100:.0f}%")
    print(SEP)

    pair_results = []

    for row in PAIRS:
        (label, symA, yf_A, lotA_unit, symB, yf_B, lotB_unit,
         lookback_in, entry_z_in, stop_z_in, annual_stop_in) = row

        recalibrate = (lookback_in is None)
        print(f"\n{SEP2}")
        print(f"  {label}  {'[RECALIBRATING]' if recalibrate else '[fixed params]'}")

        try:
            sA_raw, sB_raw = load_pair(label, symA, yf_A, symB, yf_B)
            df = pd.DataFrame({symA: sA_raw, symB: sB_raw}).dropna()
            if len(df) < 500:
                print(f"  SKIP: only {len(df)} rows")
                continue

            pa    = df[symA].values
            pb    = df[symB].values
            dates = df.index
            print(f"  Data: {dates[0].date()} → {dates[-1].date()}  ({len(df)} rows)")

            # Full-period OLS beta → lot sweep → max_hl
            res         = OLS(pa, add_constant(pb)).fit()
            _, beta     = res.params
            spread      = pa - beta * pb
            phi         = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
            hl_full     = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
            max_hl      = hl_full * 2.0

            n_A, n_B, imb = find_optimal_lots(beta, lotA_unit, lotB_unit)
            n_A_sh        = n_A * lotA_unit
            n_B_sh        = n_B * lotB_unit
            print(f"  β={beta:.4f}  HL={hl_full:.1f}d  "
                  f"Lots: {n_A}×A({lotA_unit}) + {n_B}×B({lotB_unit})  imb={imb:.1f}%")
            print(f"  Shares: {symA}={n_A_sh}  {symB}={n_B_sh}")

            avg_a = pa[-252:].mean()
            avg_b = pb[-252:].mean()
            cap   = int((avg_a * n_A_sh + avg_b * n_B_sh) * SPAN_RATE)
            print(f"  SPAN capital: Rs{cap:,}")

            if recalibrate:
                # ── Derive lookback from hl_full ─────────────────────────────
                lookback = max(int(hl_full * 2), 63)
                print(f"  Recal: LB={lookback} (from HL={hl_full:.1f})")
                print(f"  Computing signals...")
                zs, hls = compute_signals(pa, pb, lookback)

                # ── V1 permissive → annual_stop ──────────────────────────────
                v1 = simulate(pa, pb, dates, zs, hls, n_A_sh, n_B_sh,
                              entry_z=2.0, stop_z=3.5,
                              annual_stop=9_999_999, max_hl=max_hl)
                losses     = v1[v1["net_pnl"] < 0]["net_pnl"] if not v1.empty else pd.Series([])
                avg_loss   = losses.mean() if len(losses) > 0 else -20_000
                annual_stop = max(int(abs(avg_loss) * 3), 20_000)
                print(f"  V1: {len(v1)} trades  avg_loss=Rs{avg_loss:,.0f}  "
                      f"→ ANNUAL_STOP=Rs{annual_stop:,}")

                # ── Param sweep → best Sharpe ────────────────────────────────
                best = None
                for ez in ENTRY_ZS:
                    for sz in STOP_ZS:
                        if sz <= ez:
                            continue
                        tdf = simulate(pa, pb, dates, zs, hls, n_A_sh, n_B_sh,
                                       entry_z=ez, stop_z=sz,
                                       annual_stop=annual_stop, max_hl=max_hl)
                        sh, _, _ = sharpe_from_trades(tdf, cap)
                        if best is None or sh > best[2]:
                            best = (ez, sz, sh, tdf)

                entry_z, stop_z, best_sh, trades = best
                print(f"  Best: EZ={entry_z}  SZ={stop_z}  Sharpe={best_sh:.3f}  "
                      f"ANNUAL_STOP=Rs{annual_stop:,}")

            else:
                # ── Use pre-calibrated params ────────────────────────────────
                lookback    = lookback_in
                entry_z     = entry_z_in
                stop_z      = stop_z_in
                annual_stop = annual_stop_in
                print(f"  Calibrated: LB={lookback}  EZ={entry_z}  "
                      f"SZ={stop_z}  ANN_STOP=Rs{annual_stop:,}")
                print(f"  Computing signals (LB={lookback})...")
                zs, hls = compute_signals(pa, pb, lookback)
                trades  = simulate(pa, pb, dates, zs, hls, n_A_sh, n_B_sh,
                                   entry_z=entry_z, stop_z=stop_z,
                                   annual_stop=annual_stop, max_hl=max_hl)

            if trades.empty:
                print(f"  No trades — skipping")
                continue

            net_pnl = trades["net_pnl"].sum()
            n_tr    = len(trades)
            wins    = (trades["net_pnl"] > 0).sum()
            sharpe, maxdd, cagr_p = sharpe_from_trades(trades, cap)
            print(f"  Trades={n_tr}  Win={wins/n_tr*100:.1f}%  "
                  f"P&L=Rs{net_pnl:,.0f}  Sharpe={sharpe:.3f}  CAGR={cagr_p:.1f}%")

            full_idx  = pd.date_range(dates[0], dates[-1], freq="B")
            daily_pnl = build_daily_pnl(trades, full_idx)
            daily_cap = build_daily_cap(trades, df[symA], df[symB], n_A_sh, n_B_sh)
            daily_cap = daily_cap.reindex(full_idx, fill_value=0.0)
            deployed  = (daily_cap > 0)

            pair_results.append(dict(
                label=label, recalibrated=recalibrate,
                n_A_sh=n_A_sh, n_B_sh=n_B_sh,
                lotA_unit=lotA_unit, lotB_unit=lotB_unit,
                n_A=n_A, n_B=n_B, imb=imb,
                cap=cap, net_pnl=net_pnl, sharpe=sharpe,
                cagr=cagr_p, maxdd=maxdd,
                n_trades=n_tr, win_rate=wins/n_tr*100,
                lookback=lookback, entry_z=entry_z,
                stop_z=stop_z, annual_stop=annual_stop,
                daily_pnl=daily_pnl,
                daily_cap=daily_cap,
                deployed=deployed,
            ))

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()

    if not pair_results:
        print("No pairs processed successfully.")
        sys.exit(1)

    # ── Per-pair summary table ─────────────────────────────────────────────────
    print(f"\n\n{SEP}")
    print(f"  PAIR SUMMARY ({len(pair_results)} pairs)")
    print(SEP)
    hdr = (f"  {'Pair':<28} {'':>4} {'Lots A/B':>12} {'Imb%':>5} "
           f"{'SPAN Cap':>10} {'Net P&L':>13} {'Win%':>6} {'Sharpe':>7} {'CAGR%':>6}")
    print(hdr)
    print(f"  {SEP2}")
    total_span_cap = 0
    total_net_pnl  = 0
    for pr in pair_results:
        tag  = " [R]" if pr["recalibrated"] else "    "
        lots = f"{pr['n_A']}×{pr['lotA_unit']}/{pr['n_B']}×{pr['lotB_unit']}"
        sign = "+" if pr["net_pnl"] >= 0 else "-"
        print(f"  {pr['label']:<28}{tag} {lots:>12} {pr['imb']:>4.1f}% "
              f"Rs{pr['cap']/1e5:>7.2f}L  "
              f"{sign}Rs{abs(pr['net_pnl'])/1e5:>7.2f}L  "
              f"{pr['win_rate']:>5.1f}%  {pr['sharpe']:>6.3f}  "
              f"{pr['cagr']:>5.1f}%")
        total_span_cap += pr["cap"]
        total_net_pnl  += pr["net_pnl"]
    print(f"  {SEP2}")
    print(f"  {'PORTFOLIO TOTAL':<28}     {'':>12} {'':>5} "
          f"Rs{total_span_cap/1e5:>7.2f}L  "
          f"+Rs{total_net_pnl/1e5:>7.2f}L")

    # ── Build portfolio-level daily series ─────────────────────────────────────
    all_dates = set()
    for pr in pair_results:
        all_dates.update(pr["daily_pnl"].index)
    port_idx = pd.DatetimeIndex(sorted(all_dates))

    port_pnl = pd.Series(0.0, index=port_idx)
    port_cap = pd.Series(0.0, index=port_idx)
    port_dep = pd.Series(False, index=port_idx)

    for pr in pair_results:
        port_pnl += pr["daily_pnl"].reindex(port_idx, fill_value=0.0)
        port_cap += pr["daily_cap"].reindex(port_idx, fill_value=0.0)
        port_dep |= pr["deployed"].reindex(port_idx, fill_value=False)

    # Running portfolio equity (for drawdown calculations)
    port_equity = total_span_cap + port_pnl.cumsum()

    # ── Year-by-year portfolio table ───────────────────────────────────────────
    print(f"\n\n{SEP}")
    print(f"  PORTFOLIO YEAR-BY-YEAR")
    print(SEP)
    hdr2 = (f"  {'Year':<6} {'P&L':>13} {'MaxCapUsed':>13} "
            f"{'Ret/MaxCap':>11} {'MaxDD/MaxCap':>13} {'Dep%':>6} {'AvgCap%ofMax':>13} {'Active':>7}")
    print(hdr2)
    print(f"  {SEP2}")

    years   = sorted(set(port_idx.year))
    yr_rows = []

    for yr in years:
        mask  = (port_idx.year == yr)
        ypnl  = port_pnl[mask].sum()
        ymaxc = port_cap[mask].max()
        ydep  = port_dep[mask].mean() * 100

        # Return on max cap
        rmc   = (ypnl / ymaxc * 100) if ymaxc > 0 else 0.0

        # Within-year max drawdown on the running equity, expressed as % of max cap
        yr_eq        = port_equity[mask]
        yr_dd_abs    = (yr_eq - yr_eq.cummax()).min() if len(yr_eq) > 1 else 0.0
        yr_dd_maxcap = (yr_dd_abs / ymaxc * 100) if ymaxc > 0 else 0.0

        # Avg daily SPAN when any capital was deployed, as % of year's max cap
        yr_cap_deployed = port_cap[mask]
        yr_avg_cap      = yr_cap_deployed[yr_cap_deployed > 0].mean() if (yr_cap_deployed > 0).any() else 0.0
        yr_avg_util     = (yr_avg_cap / ymaxc * 100) if ymaxc > 0 else 0.0

        n_act = sum(
            1 for pr in pair_results
            if pr["daily_cap"].reindex(port_idx)[mask].max() > 0
        )
        sign  = "+" if ypnl >= 0 else "-"
        mc_s  = f"Rs{ymaxc/1e5:.2f}L"   if ymaxc > 0 else "—"
        rmc_s = f"{rmc:+.1f}%"           if ymaxc > 0 else "—"
        dd_s  = f"{yr_dd_maxcap:.1f}%"   if ymaxc > 0 else "—"
        ut_s  = f"{yr_avg_util:.1f}%"    if ymaxc > 0 else "—"
        print(f"  {yr:<6} {sign}Rs{abs(ypnl)/1e5:>8.2f}L   "
              f"{mc_s:>13} {rmc_s:>11} {dd_s:>13} {ydep:>5.1f}%  {ut_s:>13} {n_act:>6}")
        yr_rows.append(dict(year=yr, pnl=round(ypnl), max_cap=round(ymaxc),
                            dep_pct=round(ydep, 1), ret_maxcap=round(rmc, 2),
                            maxdd_maxcap=round(yr_dd_maxcap, 2),
                            avg_util_pct=round(yr_avg_util, 1), n_active=n_act))

    print(f"  {SEP2}")

    # ── Overall portfolio stats ────────────────────────────────────────────────
    START    = port_idx[0]; END = port_idx[-1]
    nyr      = max((END - START).days / 365.25, 0.1)
    peak_cap = port_cap.max()   # highest single-day total SPAN margin in history
    avg_cap  = port_cap[port_cap > 0].mean()  # avg SPAN when any position is open

    # CAGR on allocated capital: conservative (full theoretical sum, prices at today's level)
    dr          = port_pnl / total_span_cap
    p_sharpe    = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0.0
    p_maxdd     = ((port_equity - port_equity.cummax()) / port_equity.cummax() * 100).min()
    cagr_alloc  = ((max(port_equity.iloc[-1], 0.01) / total_span_cap) ** (1/nyr) - 1) * 100

    # CAGR on peak deployed capital: what you actually needed at the high-water mark
    peak_equity = peak_cap + port_pnl.cumsum().iloc[-1]
    cagr_peak   = ((max(peak_equity, 0.01) / peak_cap) ** (1/nyr) - 1) * 100

    # CAGR on avg deployed capital: most realistic day-to-day view
    avg_equity  = avg_cap + port_pnl.cumsum().iloc[-1]
    cagr_avg    = ((max(avg_equity, 0.01) / avg_cap) ** (1/nyr) - 1) * 100

    dep_all  = port_dep.mean() * 100

    print(f"\n  OVERALL PORTFOLIO")
    print(f"  {SEP2}")
    print(f"  Pairs active          : {len(pair_results)}")
    print(f"  Total Net P&L         : Rs{total_net_pnl:,.0f}")
    print(f"  Sharpe                : {p_sharpe:.3f}")
    print(f"  Max Drawdown          : {p_maxdd:.2f}%")
    print(f"  Avg deploy%           : {dep_all:.1f}% of days")
    print(f"  Period                : {START.date()} → {END.date()}  ({nyr:.1f} yr)")
    print(f"")
    print(f"  CAGR (3 bases — same P&L, different denominator):")
    print(f"  ├─ On sum-of-SPAN  Rs{total_span_cap/1e5:.1f}L  → {cagr_alloc:.1f}%  "
          f"(conservative: as if full capital locked from day 1)")
    print(f"  ├─ On peak SPAN    Rs{peak_cap/1e5:.1f}L  → {cagr_peak:.1f}%  "
          f"(realistic: max capital you ever actually needed)")
    print(f"  └─ On avg SPAN     Rs{avg_cap/1e5:.1f}L  → {cagr_avg:.1f}%  "
          f"(deployed: avg margin while in any position)")
    print(f"\n  [R] = recalibrated at new lot sizes via V1→sweep")

    # ── Save output ────────────────────────────────────────────────────────────
    summary = dict(
        n_pairs=len(pair_results), total_span_cap=round(total_span_cap),
        peak_span=round(peak_cap), avg_span=round(avg_cap),
        total_net_pnl=round(total_net_pnl),
        cagr_alloc=round(cagr_alloc,2), cagr_peak=round(cagr_peak,2),
        cagr_avg=round(cagr_avg,2),
        sharpe=round(p_sharpe,3), maxdd=round(p_maxdd,2),
        dep_pct=round(dep_all,1), start=str(START.date()), end=str(END.date()),
        n_years=round(nyr,1),
    )
    pd.DataFrame([summary]).to_csv(RESULTS_DIR / "portfolio_summary_v2.csv", index=False)

    # ── Save daily equity series ───────────────────────────────────────────────
    dd_series = (port_equity - port_equity.cummax()) / port_equity.cummax() * 100
    daily_df  = pd.DataFrame({
        "date":         port_idx.strftime("%Y-%m-%d"),
        "equity":       port_equity.round(0).astype(int).values,
        "drawdown_pct": dd_series.round(4).values,
        "span_cap":     port_cap.round(0).astype(int).values,
    })
    daily_path = RESULTS_DIR / "portfolio_daily_equity.csv"
    daily_df.to_csv(daily_path, index=False)
    print(f"  Saved daily equity  : {daily_path}")

    # ── Matplotlib equity curve + drawdown ────────────────────────────────────
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    dates_plot = pd.to_datetime(port_idx)
    eq_l  = port_equity / 1e5
    base  = total_span_cap / 1e5

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8),
                                    gridspec_kw={"height_ratios": [3, 1]},
                                    sharex=True)
    fig.patch.set_facecolor("#ffffff")

    ax1.fill_between(dates_plot, base, eq_l, alpha=0.15, color="#1D9E75")
    ax1.plot(dates_plot, eq_l, color="#1D9E75", linewidth=1.5)
    ax1.axhline(base, color="#888", linewidth=0.8, linestyle="--", alpha=0.6,
                label=f"Initial capital Rs{base:.0f}L")
    ax1.set_ylabel("Portfolio equity (Rs L)", fontsize=11)
    ax1.set_title("10-Pair Portfolio — Equity Curve & Drawdown  (2015–2026)",
                  fontsize=13, fontweight="normal", pad=12)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"Rs{x:.0f}L"))
    ax1.legend(fontsize=9, framealpha=0.4)
    ax1.grid(True, alpha=0.2, linewidth=0.5)
    ax1.set_facecolor("#fafafa")

    ax2.fill_between(dates_plot, dd_series.values, 0, alpha=0.4, color="#D85A30")
    ax2.plot(dates_plot, dd_series.values, color="#D85A30", linewidth=0.9)
    ax2.set_ylabel("Drawdown %", fontsize=11)
    ax2.set_ylim(dd_series.min() * 1.3, 1)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.grid(True, alpha=0.2, linewidth=0.5)
    ax2.set_facecolor("#fafafa")

    plt.tight_layout()
    plot_path = RESULTS_DIR / "portfolio_equity_curve.png"
    fig.savefig(plot_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"  Saved equity curve  : {plot_path}")

    yr_df   = pd.DataFrame(yr_rows)
    pair_df = pd.DataFrame([
        dict(label=pr["label"], recalibrated=pr["recalibrated"],
             lookback=pr["lookback"], entry_z=pr["entry_z"],
             stop_z=pr["stop_z"], annual_stop=pr["annual_stop"],
             n_A=pr["n_A"], lotA=pr["lotA_unit"], n_A_sh=pr["n_A_sh"],
             n_B=pr["n_B"], lotB=pr["lotB_unit"], n_B_sh=pr["n_B_sh"],
             imb=round(pr["imb"], 1), span_cap=pr["cap"],
             net_pnl=pr["net_pnl"], sharpe=pr["sharpe"],
             cagr=pr["cagr"], maxdd=pr["maxdd"],
             n_trades=pr["n_trades"], win_rate=round(pr["win_rate"], 1))
        for pr in pair_results
    ])
    yr_df.to_csv(RESULTS_DIR / "portfolio_yearly_v2.csv", index=False)
    pair_df.to_csv(RESULTS_DIR / "portfolio_pairs_summary_v2.csv", index=False)
    print(f"\n  Saved: portfolio_yearly_v2.csv  |  portfolio_pairs_summary_v2.csv")
    print(SEP)
