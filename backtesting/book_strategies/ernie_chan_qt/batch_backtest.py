"""
batch_backtest.py  v2
Parallel backtest — 8 pairs.
Auto lot-balance: sweeps n=1..10 for Leg A, picks n with global minimum imbalance.
Added metrics: yearly max capital used (SPAN), return on that max capital.
Imbalance shown in every result table.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np
from statsmodels.tsa.stattools import adfuller
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
import yfinance as yf
import multiprocessing as mp
import traceback

STOCK_DIR   = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/data/stocks"
RESULTS_DIR = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

COOLDOWN   = 5
EXIT_Z     = 0.5
ENTRY_ZS   = [1.5, 2.0, 2.5]
STOP_ZS    = [3.0, 3.5, 4.0]
MIN_ROWS   = 1500
MAX_LOT_N  = 10
SPAN_RATE  = 0.15

# (label, symA, yf_A, lot_A_size, symB, yf_B, lot_B_size)
# lot_X_size = shares per 1 NSE F&O lot
PAIRS = [
    ("HDFCBANK/KOTAKBANK",  "HDFCBANK",  "HDFCBANK.NS",   550, "KOTAKBANK",  "KOTAKBANK.NS",   400),
    ("HINDUNILVR/DABUR",    "HINDUNILVR","HINDUNILVR.NS",  300, "DABUR",      "DABUR.NS",       1250),
    ("OBEROIRLTY/BRIGADE",  "OBEROIRLTY","OBEROIRLTY.NS",  800, "BRIGADE",    "BRIGADE.NS",     1000),
    ("TATAPOWER/JSWENERGY", "TATAPOWER", "TATAPOWER.NS", 4950, "JSWENERGY",  "JSWENERGY.NS",   2000),
    ("TECHM/COFORGE",       "TECHM",     "TECHM.NS",      600, "COFORGE",    "COFORGE.NS",      200),
    ("EICHERMOT/TVSMOTORS", "EICHERMOT", "EICHERMOT.NS",  200, "TVSMOTORS",  "TVSMOTOR.NS",     350),
    ("HDFCLIFE/ICICIPRULI", "HDFCLIFE",  "HDFCLIFE.NS",  1100, "ICICIPRULI", "ICICIPRULI.NS",  1500),
    ("SRF/DEEPAKNTR",       "SRF",       "SRF.NS",        250, "DEEPAKNTR",  "DEEPAKNTR.NS",    350),
]


def find_optimal_lots(beta, lot_a_size, lot_b_size, max_n=MAX_LOT_N):
    """
    Sweep n=1..max_n lots of Leg A.
    Return (n_A, n_B, imb_pct) with the globally lowest imbalance.
    """
    best = None
    for n in range(1, max_n + 1):
        ideal_b  = beta * lot_a_size * n
        n_b      = max(1, round(ideal_b / lot_b_size))
        actual_b = n_b * lot_b_size
        imb      = abs(actual_b - ideal_b) / ideal_b * 100
        if best is None or imb < best[2]:
            best = (n, n_b, imb)
    return best


def load_stock(sym, yf_ticker):
    for suffix in ("_ext", "_yf"):
        cache = STOCK_DIR / f"{sym}{suffix}.parquet"
        if cache.exists():
            raw = pd.read_parquet(cache)
            s   = raw.iloc[:, 0] if isinstance(raw, pd.DataFrame) else raw
            s.index = pd.to_datetime(s.index)
            return s.sort_index().dropna()
    df = yf.download(yf_ticker, start="2015-01-01", end="2024-05-28",
                     progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No data for {yf_ticker}")
    s = df["Close"].squeeze()
    s.index = pd.to_datetime(s.index)
    s = s.sort_index().dropna()
    if len(s) >= MIN_ROWS:
        s.to_frame(name=sym).to_parquet(STOCK_DIR / f"{sym}_yf.parquet")
    return s


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


def simulate(pa, pb, dates, zscores, half_lives, lot_a, lot_b,
             entry_z, exit_z, stop_z, annual_stop, max_hl):
    BROK = 0.0003

    def pnl(pos, epa, epb, xpa, xpb):
        gross = ((xpa - epa) * lot_a - (xpb - epb) * lot_b) * pos
        cost  = (epa*lot_a + epb*lot_b + xpa*lot_a + xpb*lot_b) * BROK
        return gross - cost

    pos = 0; epa = epb = 0.0; ebar = 0
    yrpnl = 0.0; curyr = dates[0].year; cdend = 0
    trades = []

    for t in range(len(pa)):
        if np.isnan(zscores[t]):
            continue
        if dates[t].year != curyr:
            curyr = dates[t].year
            yrpnl = 0.0
        z, hl = zscores[t], half_lives[t]

        if pos != 0:
            mtm = ((pa[t] - epa) * lot_a - (pb[t] - epb) * lot_b) * pos
            er  = None
            if pos ==  1 and z >= -exit_z:         er = "z_exit"
            if pos == -1 and z <=  exit_z:         er = "z_exit"
            if abs(z) >= stop_z:                   er = "z_stop"
            if (yrpnl + mtm) < -annual_stop:       er = "annual_stop"
            if er:
                net    = pnl(pos, epa, epb, pa[t], pb[t])
                yrpnl += net
                trades.append(dict(
                    entry_date=dates[ebar], exit_date=dates[t],
                    hold_days=(dates[t] - dates[ebar]).days,
                    direction="LongA" if pos == 1 else "ShortA",
                    net_pnl=round(net, 2), exit_reason=er,
                    z_entry=round(zscores[ebar], 3), z_exit=round(z, 3),
                ))
                pos = 0; cdend = t + COOLDOWN
            continue

        if t < cdend or yrpnl < -annual_stop or hl > max_hl:
            continue
        if z < -entry_z:
            pos = 1;  epa = pa[t]; epb = pb[t]; ebar = t
        elif z > entry_z:
            pos = -1; epa = pa[t]; epb = pb[t]; ebar = t

    if pos != 0:
        t   = len(pa) - 1
        net = pnl(pos, epa, epb, pa[t], pb[t])
        trades.append(dict(
            entry_date=dates[ebar], exit_date=dates[t],
            hold_days=(dates[t] - dates[ebar]).days,
            direction="LongA" if pos == 1 else "ShortA",
            net_pnl=round(net, 2), exit_reason="end_of_data",
            z_entry=round(zscores[ebar], 3), z_exit=round(z, 3),
        ))
    return pd.DataFrame(trades)


def sharpe_dd_cagr(trades_df, capital):
    if trades_df.empty:
        return 0.0, 0.0, 0.0
    START = trades_df["entry_date"].min()
    END   = trades_df["exit_date"].max()
    idx   = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    eq     = capital + daily.cumsum()
    dr     = daily / capital
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else 0.0
    maxdd  = ((eq - eq.cummax()) / eq.cummax() * 100).min()
    nyr    = max((END - START).days / 365.25, 0.1)
    end_eq = max(eq.iloc[-1], 0.01)
    cagr   = ((end_eq / capital) ** (1 / nyr) - 1) * 100
    return round(sharpe, 3), round(maxdd, 2), round(cagr, 2)


def max_cap_per_year(trades_df, sA, sB, n_A_shares, n_B_shares):
    """
    Daily SPAN margin = 15% of notional while a position is open.
    Returns {year: peak_margin_that_year}.
    """
    if trades_df.empty:
        return {}
    cap_daily = pd.Series(0.0, index=sA.index)
    for _, tr in trades_df.iterrows():
        mask     = (sA.index >= tr["entry_date"]) & (sA.index <= tr["exit_date"])
        notional = SPAN_RATE * (n_A_shares * sA[mask] + n_B_shares * sB[mask])
        cap_daily.loc[mask] = np.maximum(cap_daily.loc[mask].values, notional.values)
    result = {}
    for yr, grp in cap_daily.groupby(cap_daily.index.year):
        mx = grp.max()
        if mx > 0:
            result[int(yr)] = mx
    return result


def run_pair(cfg):
    label, symA, yf_A, lotA_size, symB, yf_B, lotB_size = cfg
    out = []
    SEP = "=" * 76

    try:
        out.append(f"\n{SEP}\n  {label}\n{SEP}")

        sA_raw = load_stock(symA, yf_A)
        sB_raw = load_stock(symB, yf_B)
        df = pd.DataFrame({symA: sA_raw, symB: sB_raw}).dropna()
        if len(df) < MIN_ROWS:
            out.append(f"  SKIP: only {len(df)} rows (< {MIN_ROWS})")
            return label, None, "\n".join(out)

        pa    = df[symA].values
        pb    = df[symB].values
        dates = df.index
        out.append(f"  Data: {dates[0].date()} → {dates[-1].date()}  ({len(df)} rows)")

        # Full-period cointegration
        res         = OLS(pa, add_constant(pb)).fit()
        alpha, beta = res.params
        spread      = pa - beta * pb
        adf         = adfuller(spread, autolag="AIC")
        stat, pval  = adf[0], adf[1]
        crit        = adf[4]
        level       = ("1%"  if stat < crit["1%"] else
                       "5%"  if stat < crit["5%"] else
                       "10%" if stat < crit["10%"] else "FAIL")
        out.append(f"  ADF={stat:.3f}  p={pval:.4f}  [{level}]  "
                   f"R²={res.rsquared:.3f}  β={beta:.4f}")
        if pval > 0.12:
            out.append("  → NOT COINTEGRATED. Skip.")
            return label, None, "\n".join(out)

        # Half-life → LOOKBACK
        phi      = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
        hl       = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
        lookback = max(int(hl * 2), 63)
        max_hl   = hl * 2.0
        out.append(f"  Half-life={hl:.1f}d  LOOKBACK={lookback}  max_HL={max_hl:.0f}d")

        # ── Auto lot-balance: sweep n=1..10, pick minimum imbalance ─────────────
        n_A, n_B, imb = find_optimal_lots(beta, lotA_size, lotB_size)
        n_A_shares    = n_A * lotA_size
        n_B_shares    = n_B * lotB_size
        ideal_b       = beta * n_A_shares
        out.append(f"  Lot sweep (n=1..{MAX_LOT_N}): best = {n_A}×A + {n_B}×B  "
                   f"ideal_B={ideal_b:.0f}  actual_B={n_B_shares}  imb={imb:.1f}%")
        out.append(f"  Lots     : {symA} {n_A}×{lotA_size}={n_A_shares}sh  "
                   f"{symB} {n_B}×{lotB_size}={n_B_shares}sh")

        avg_a = pa[-252:].mean()
        avg_b = pb[-252:].mean()
        cap   = int((avg_a * n_A_shares + avg_b * n_B_shares) * SPAN_RATE)
        out.append(f"  Capital (15% SPAN): Rs{cap:,}")

        out.append(f"  Computing signals...")
        zscores, half_lives = compute_signals(pa, pb, lookback)
        out.append(f"  Signals done.")

        # V1 (permissive) → derive ANNUAL_STOP
        v1 = simulate(pa, pb, dates, zscores, half_lives,
                      n_A_shares, n_B_shares,
                      entry_z=2.0, exit_z=EXIT_Z, stop_z=3.5,
                      annual_stop=9_999_999, max_hl=max_hl)
        avg_loss    = (v1[v1["net_pnl"] < 0]["net_pnl"].mean()
                       if not v1.empty and (v1["net_pnl"] < 0).any() else -10_000)
        annual_stop = max(int(abs(avg_loss) * 3), 20_000)
        out.append(f"  V1: {len(v1)} trades  avg_loss=Rs{avg_loss:,.0f}  "
                   f"ANNUAL_STOP=Rs{annual_stop:,}")

        # Parameter sweep → best Sharpe
        best = None
        for ez in ENTRY_ZS:
            for sz in STOP_ZS:
                if sz <= ez:
                    continue
                tdf = simulate(pa, pb, dates, zscores, half_lives,
                               n_A_shares, n_B_shares,
                               entry_z=ez, exit_z=EXIT_Z, stop_z=sz,
                               annual_stop=annual_stop, max_hl=max_hl)
                sh, _, _ = sharpe_dd_cagr(tdf, cap)
                if best is None or sh > best[2]:
                    best = (ez, sz, sh, tdf)

        best_ez, best_sz, best_sh, best_tdf = best
        sharpe, maxdd, cagr = sharpe_dd_cagr(best_tdf, cap)
        out.append(f"  Best: ENTRY_Z={best_ez}  STOP_Z={best_sz}  Sharpe={sharpe:.3f}")

        if best_tdf.empty:
            out.append("  No trades generated.")
            return label, None, "\n".join(out)

        bt       = best_tdf
        net_pnl  = bt["net_pnl"].sum()
        n_trades = len(bt)
        wins     = (bt["net_pnl"] > 0).sum()
        win_rate = wins / n_trades * 100
        gw       = bt[bt["net_pnl"] > 0]["net_pnl"].sum()
        gl       = bt[bt["net_pnl"] < 0]["net_pnl"].sum()
        pf       = gw / abs(gl) if gl != 0 else 99.0
        avg_hold = bt["hold_days"].mean()

        START = bt["entry_date"].min()
        END   = bt["exit_date"].max()
        nyr   = (END - START).days / 365.25

        full_idx  = pd.date_range(dates[0], dates[-1], freq="B")
        daily_all = pd.Series(0.0, index=full_idx)
        for _, tr in bt.iterrows():
            if tr["exit_date"] in daily_all.index:
                daily_all[tr["exit_date"]] += tr["net_pnl"]
        eq_all = cap + daily_all.cumsum()

        dep_s = pd.Series(False, index=full_idx)
        for _, tr in bt.iterrows():
            dep_s[(full_idx >= tr["entry_date"]) & (full_idx <= tr["exit_date"])] = True
        dep_pct = dep_s.mean() * 100
        dep_ret = (net_pnl / nyr) / (cap * dep_s.mean()) * 100 if dep_s.mean() > 0 else 0

        stop_exits = (bt["exit_reason"] == "z_stop").sum()
        yr_totals  = bt.groupby(bt["exit_date"].dt.year)["net_pnl"].sum()
        fired_yrs  = [str(y) for y, p in yr_totals.items() if p < -annual_stop]

        verdict = ("PASS"     if sharpe > 0.35 and net_pnl > 0 else
                   "MARGINAL" if net_pnl > 0                    else "FAIL")

        # ── Max capital per year ─────────────────────────────────────────────────
        yr_maxcap = max_cap_per_year(bt, df[symA], df[symB], n_A_shares, n_B_shares)

        D = "─" * 76
        out.append(f"\n  {D}")
        out.append(f"  RESULT [{verdict}]  imb={imb:.1f}%")
        out.append(f"  {D}")
        out.append(f"  Params   : LOOKBACK={lookback}  ENTRY_Z={best_ez}  "
                   f"STOP_Z={best_sz}  ANNUAL_STOP=Rs{annual_stop:,}")
        out.append(f"  Lots     : {symA}={n_A_shares}sh ({n_A}×{lotA_size})  "
                   f"{symB}={n_B_shares}sh ({n_B}×{lotB_size})  imb={imb:.1f}%")
        out.append(f"  Capital  : Rs{cap:,}  (15% SPAN on avg last-year prices)")
        out.append(f"  Period   : {START.date()} → {END.date()}  ({nyr:.1f} yr)")
        out.append(f"  Net P&L  : Rs{net_pnl:,.0f}")
        out.append(f"  CAGR     : {cagr:.2f}%")
        out.append(f"  Sharpe   : {sharpe:.3f}")
        out.append(f"  Max DD   : {maxdd:.2f}%")
        out.append(f"  Win Rate : {win_rate:.1f}%  ({wins}W / {n_trades-wins}L)")
        out.append(f"  Pft Fac  : {pf:.2f}")
        out.append(f"  Trades   : {n_trades}  avg hold={avg_hold:.1f}d")
        out.append(f"  Deploy   : {dep_pct:.1f}% of days  dep_ret={dep_ret:.1f}%/yr")
        out.append(f"  Z-stops  : {stop_exits}/{n_trades}")
        out.append(f"  Ann stop : {fired_yrs if fired_yrs else 'NEVER fired'}")

        # ── Year-by-year table ───────────────────────────────────────────────────
        out.append(f"\n  {D}")
        out.append(f"  YEAR-BY-YEAR")
        out.append(f"  {D}")
        hdr = (f"  {'Year':<6} {'P&L':>11} {'Ret%':>7} "
               f"{'MaxCapUsed':>12} {'Ret/MaxCap':>11} "
               f"{'Tr':>4} {'Win%':>6} {'MaxDD%':>8}")
        out.append(hdr)
        out.append(f"  {D}")

        for yr in sorted(bt["exit_date"].dt.year.unique()):
            yr_bt  = bt[bt["exit_date"].dt.year == yr]
            yr_pnl = yr_bt["net_pnl"].sum()
            yr_n   = len(yr_bt)
            yr_w   = (yr_bt["net_pnl"] > 0).sum()
            yr_eq  = eq_all[full_idx.year == yr]
            yr_dd  = (((yr_eq - yr_eq.cummax()) / yr_eq.cummax() * 100).min()
                      if len(yr_eq) > 1 else 0.0)
            sign   = "+Rs" if yr_pnl >= 0 else "-Rs"
            wr_s   = f"{yr_w/yr_n*100:.0f}%" if yr_n > 0 else "—"
            mc     = yr_maxcap.get(yr, 0)
            rmc    = f"{yr_pnl/mc*100:+.1f}%" if mc > 0 else "—"
            mc_str = f"Rs{mc/100000:.2f}L"   if mc > 0 else "—"
            out.append(f"  {yr:<6} {sign}{abs(yr_pnl):>8,.0f} "
                       f"{yr_pnl/cap*100:>+7.1f}%  "
                       f"{mc_str:>12} {rmc:>11}  "
                       f"{yr_n:>3}  {wr_s:>5}  {yr_dd:>+7.2f}%")
        out.append(f"  {D}")

        result = dict(
            label=label, verdict=verdict,
            sharpe=sharpe, cagr=cagr, maxdd=maxdd,
            capital=cap, net_pnl=int(net_pnl),
            n_trades=n_trades, win_rate=round(win_rate, 1),
            pf=round(pf, 2), avg_hold=round(avg_hold, 1),
            dep_pct=round(dep_pct, 1),
            imb=round(imb, 1),
            n_A=n_A, n_A_shares=n_A_shares,
            n_B=n_B, n_B_shares=n_B_shares,
            lookback=lookback, entry_z=best_ez, stop_z=best_sz,
            annual_stop=annual_stop,
            annual_stop_fired=bool(fired_yrs),
        )
        return label, result, "\n".join(out)

    except Exception as e:
        out.append(f"  ERROR: {e}")
        out.append(traceback.format_exc())
        return label, None, "\n".join(out)


if __name__ == "__main__":
    SEP = "=" * 76
    print(SEP)
    print(f"  BATCH BACKTEST v2 — {len(PAIRS)} pairs — auto lot-balance — parallel")
    print(f"  CPUs: {mp.cpu_count()}   Lot sweep: n=1..{MAX_LOT_N}")
    print(SEP)

    workers = min(len(PAIRS), mp.cpu_count())
    with mp.Pool(processes=workers) as pool:
        raw_results = pool.map(run_pair, PAIRS)

    summaries = []
    for label, result, output in raw_results:
        print(output)
        if result:
            summaries.append(result)

    # ── Comparison table ───────────────────────────────────────────────────────
    print(f"\n\n{SEP}")
    print("  COMPARISON TABLE  (sorted by Sharpe)")
    print(SEP)
    print(f"  {'Pair':<24} {'V':<9} {'Sharpe':>7} {'CAGR%':>7} "
          f"{'MaxDD%':>8} {'Capital':>9} {'Imb%':>6} "
          f"{'N':>5} {'Win%':>6} {'Lots (A×B)':>14}")
    print(f"  {'─'*76}")

    summaries.sort(key=lambda x: x["sharpe"], reverse=True)
    for r in summaries:
        v  = "✓✓" if r["verdict"] == "PASS" else "~" if r["verdict"] == "MARGINAL" else "✗"
        la = r.get("label", "")
        parts = la.split("/")
        lot_str = f"{r['n_A']}×{r['n_A_shares']//r['n_A']} / {r['n_B']}×{r['n_B_shares']//r['n_B']}"
        print(f"  {v} {la:<22} {r['verdict']:<9} "
              f"{r['sharpe']:>7.3f} {r['cagr']:>7.2f}% "
              f"{r['maxdd']:>8.2f}%  Rs{r['capital']/100000:>4.2f}L "
              f"{r['imb']:>5.1f}%  "
              f"{r['n_trades']:>5} {r['win_rate']:>6.1f}% "
              f"{lot_str:>14}")
    print(f"  {'─'*76}")

    # ── Calibration params ─────────────────────────────────────────────────────
    pass_pairs = [r for r in summaries if r["verdict"] == "PASS"]
    if pass_pairs:
        print(f"\n{SEP}")
        print("  CALIBRATION PARAMS — PASS pairs")
        print(SEP)
        for r in pass_pairs:
            fired = "YES" if r["annual_stop_fired"] else "never"
            print(f"  {r['label']:<24} "
                  f"LOOKBACK={r['lookback']:<5} "
                  f"ENTRY_Z={r['entry_z']}  STOP_Z={r['stop_z']}  "
                  f"ANNUAL_STOP=Rs{r['annual_stop']:,}  "
                  f"stop_fired={fired}  imb={r['imb']:.1f}%")

    if summaries:
        out_path = RESULTS_DIR / "batch_backtest_8pairs.csv"
        pd.DataFrame(summaries).to_csv(out_path, index=False)
        print(f"\n  Results saved → {out_path}")

    print(f"\n{SEP}\n  DONE\n{SEP}")
