"""
Calibrate new candidate pairs using the same V1->V3 pipeline as portfolio_backtest.py.
Data loaded from Nifty 500 Daily Data folder (no yfinance needed).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd
import itertools
from pathlib import Path
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

DATA_DIR = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
RESULTS_DIR = Path(r"G:\fyers_data_pipeline\backtesting\book_strategies\ernie_chan_qt\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

START = "2015-01-01"
END   = "2026-06-01"

# (name, sym_A, lotsize_A, sym_B, lotsize_B)
# Lot sizes: NSE F&O standard lot sizes (current)
CANDIDATES = [
    ("SIEMENS/ABB",          "SIEMENS",   275,  "ABB",        375),
    ("BPCL/HINDPETRO",       "BPCL",     1800,  "HINDPETRO", 1375),
    ("IOC/HINDPETRO",        "IOC",      3975,  "HINDPETRO", 1375),
    ("LUPIN/AUROPHARMA",     "LUPIN",     850,  "AUROPHARMA",  550),
    ("HINDALCO/JSWSTEEL",    "HINDALCO", 1225,  "JSWSTEEL",    600),
    ("GODREJCP/MARICO",      "GODREJCP",  500,  "MARICO",     1000),
]

ENTRY_ZS = [1.5, 2.0, 2.5]
STOP_ZS  = [3.0, 3.5, 4.0]


# ── Data loading ──────────────────────────────────────────────────────────────
def load_prices(symbol):
    f = DATA_DIR / f"{symbol}.parquet"
    df = pd.read_parquet(f)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index().loc[START:END]
    col = "close" if "close" in df.columns else "Close"
    return df[col].rename(symbol)


# ── Lot optimiser ─────────────────────────────────────────────────────────────
def find_optimal_lots(prices_a, prices_b, lot_a, lot_b, beta):
    ideal_b_notional = beta * lot_a * prices_a.mean()
    best_n, best_err = 1, 1e9
    for n in range(1, 30):
        actual = n * lot_b * prices_b.mean()
        err = abs(actual - ideal_b_notional) / ideal_b_notional
        if err < best_err:
            best_err, best_n = err, n
    # n_a: scale so SPAN is reasonable
    lots_b = best_n
    # find n_a that gives similar notional
    ideal_a = lots_b * lot_b * prices_b.mean() / beta / prices_a.mean()
    n_a = max(1, round(ideal_a / lot_a))
    return n_a, lots_b


# ── OLS beta + spread ─────────────────────────────────────────────────────────
def compute_ols(la, lb, lookback):
    n = len(la)
    betas = np.zeros(n) * np.nan
    for i in range(lookback, n):
        xa = la[i-lookback:i]
        xb = lb[i-lookback:i]
        ols = OLS(xb, add_constant(xa)).fit()
        betas[i] = ols.params[1]
    return betas


# ── Signal computation ────────────────────────────────────────────────────────
def compute_signals(la, lb, lookback, entry_z, stop_z):
    n = len(la)
    betas = compute_ols(la, lb, lookback)

    spread = lb - betas * la
    mu = pd.Series(spread).rolling(lookback).mean().values
    sd = pd.Series(spread).rolling(lookback).std().values
    z  = (spread - mu) / sd

    pos = np.zeros(n)
    for i in range(lookback, n):
        zi = z[i]
        prev = pos[i-1]
        if np.isnan(zi) or np.isnan(betas[i]):
            pos[i] = 0
            continue
        if prev == 0:
            if zi > entry_z:
                pos[i] = -1   # short spread
            elif zi < -entry_z:
                pos[i] = 1    # long spread
            else:
                pos[i] = 0
        elif prev == 1:
            if zi > 0 or zi < -stop_z:
                pos[i] = 0
            else:
                pos[i] = 1
        elif prev == -1:
            if zi < 0 or zi > stop_z:
                pos[i] = 0
            else:
                pos[i] = -1

    return pos, z, betas


# ── Simulation ────────────────────────────────────────────────────────────────
def simulate(dates, pa, pb, lot_a, lot_b, n_a, n_b, pos, annual_stop=None):
    qty_a = n_a * lot_a
    qty_b = n_b * lot_b

    trades = []
    daily_pnl = np.zeros(len(dates))
    entry_date = None
    entry_pa = entry_pb = None
    direction = 0
    annual_pnl = {}
    killed_year = None

    for i in range(1, len(dates)):
        yr = dates[i].year
        prev = int(pos[i-1])
        curr = int(pos[i])

        if prev != 0:
            # daily mark-to-market while in position
            dpnl = direction * qty_a * (pa[i] - pa[i-1]) - direction * qty_b * (pb[i] - pb[i-1])
            dpnl -= (abs(qty_a * (pa[i] - pa[i-1])) + abs(qty_b * (pb[i] - pb[i-1]))) * 0.0005
            daily_pnl[i] = dpnl
            annual_pnl[yr] = annual_pnl.get(yr, 0) + dpnl

        # annual stop check
        if annual_stop and yr != killed_year:
            if annual_pnl.get(yr, 0) < -annual_stop:
                killed_year = yr

        if killed_year == yr:
            pos[i] = 0
            curr = 0

        if prev == 0 and curr != 0:
            direction = curr
            entry_date = dates[i]
            entry_pa = pa[i]
            entry_pb = pb[i]
        elif prev != 0 and curr == 0:
            if entry_pa is not None:
                gross = direction * qty_a * (pa[i] - entry_pa) - direction * qty_b * (pb[i] - entry_pb)
                cost  = (qty_a * pa[i] + qty_a * entry_pa + qty_b * pb[i] + qty_b * entry_pb) * 0.0005
                pnl   = gross - cost
                hold  = (dates[i] - entry_date).days
                trades.append({"date": dates[i], "pnl": pnl, "hold_days": hold, "direction": direction})
            direction = 0

    return trades, daily_pnl


# ── SPAN capital ──────────────────────────────────────────────────────────────
def span_cap(pa, pb, lot_a, lot_b, n_a, n_b):
    return (n_a * lot_a * pa + n_b * lot_b * pb) * 0.15


# ── Sharpe from trades ────────────────────────────────────────────────────────
def sharpe_from_daily(daily_pnl):
    pnl = pd.Series(daily_pnl)
    if pnl.std() == 0:
        return 0.0
    return pnl.mean() / pnl.std() * np.sqrt(252)


# ── Run one pair ──────────────────────────────────────────────────────────────
def calibrate(name, sym_a, lot_a, sym_b, lot_b):
    print(f"\n{'='*65}")
    print(f"  CALIBRATING: {name}")
    print(f"{'='*65}")

    try:
        pa_s = load_prices(sym_a)
        pb_s = load_prices(sym_b)
    except Exception as e:
        print(f"  ERROR loading data: {e}")
        return

    df = pd.DataFrame({sym_a: pa_s, sym_b: pb_s}).dropna()
    dates = df.index.to_pydatetime()
    pa = df[sym_a].values
    pb = df[sym_b].values
    la = np.log(pa)
    lb = np.log(pb)
    N  = len(df)
    print(f"  Data: {dates[0].date()} → {dates[-1].date()}  ({N} days)")

    # ── Step 1: detect half-life ──────────────────────────────────────────────
    # Use full-period OLS to get a representative beta for lot sizing
    ols_full = OLS(lb, add_constant(la)).fit()
    beta_rep = ols_full.params[1]
    print(f"  Full-period OLS beta: {beta_rep:.4f}")

    # Half-life from AR(1) on spread differences (full period)
    spread_full = lb - beta_rep * la
    phi = OLS(pd.Series(spread_full).diff().dropna(),
              add_constant(pd.Series(spread_full).shift(1).dropna())).fit().params.iloc[1]
    hl = -np.log(2) / np.log(1 + phi) if phi < 0 else 999
    lookback = max(int(2 * hl), 63)
    print(f"  Half-life: {hl:.1f}d  →  Lookback: {lookback}")

    # Lot sizing
    n_a, n_b = find_optimal_lots(pd.Series(pa), pd.Series(pb), lot_a, lot_b, beta_rep)
    print(f"  Lot sizing: {n_a}×{lot_a} {sym_a}  vs  {n_b}×{lot_b} {sym_b}")

    # ── Step 2: V1 permissive run (entry_z=1.5, stop_z=5.0, no annual stop) ──
    print(f"\n  [V1] Permissive run...")
    pos_v1, _, _ = compute_signals(la, lb, lookback, entry_z=1.5, stop_z=5.0)
    trades_v1, dpnl_v1 = simulate(dates, pa, pb, lot_a, lot_b, n_a, n_b, pos_v1, annual_stop=None)
    if not trades_v1:
        print("  No trades — skipping pair")
        return
    losses = [t["pnl"] for t in trades_v1 if t["pnl"] < 0]
    avg_loss = abs(np.mean(losses)) if losses else 20_000
    annual_stop = max(int(3 * avg_loss), 20_000)
    net_pnl_v1 = sum(t["pnl"] for t in trades_v1)
    sharpe_v1 = sharpe_from_daily(dpnl_v1)
    print(f"  V1: {len(trades_v1)} trades  Net₹{net_pnl_v1:,.0f}  Sharpe {sharpe_v1:.3f}")
    print(f"  Avg loss: ₹{avg_loss:,.0f}  →  Annual stop: ₹{annual_stop:,.0f}")

    # ── Step 3: Grid sweep ────────────────────────────────────────────────────
    print(f"\n  [V3] Grid sweep  entry_z∈{ENTRY_ZS}  stop_z∈{STOP_ZS} ...")
    best = {"sharpe": -99, "params": None, "trades": None, "dpnl": None}
    for ez, sz in itertools.product(ENTRY_ZS, STOP_ZS):
        if sz <= ez:
            continue
        pos, _, _ = compute_signals(la, lb, lookback, entry_z=ez, stop_z=sz)
        trades, dpnl = simulate(dates, pa, pb, lot_a, lot_b, n_a, n_b, pos, annual_stop=annual_stop)
        s = sharpe_from_daily(dpnl)
        if s > best["sharpe"]:
            best = {"sharpe": s, "params": (ez, sz), "trades": trades, "dpnl": dpnl}

    if best["params"] is None:
        print("  No valid sweep result")
        return

    ez, sz = best["params"]
    trades = best["trades"]
    dpnl   = best["dpnl"]
    print(f"  Best params: entry_z={ez}  stop_z={sz}  Sharpe={best['sharpe']:.3f}")

    # ── Step 4: Report ────────────────────────────────────────────────────────
    net_pnl = sum(t["pnl"] for t in trades)
    wins    = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses2 = [t["pnl"] for t in trades if t["pnl"] < 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win  = np.mean(wins)  if wins  else 0
    avg_loss2 = np.mean(losses2) if losses2 else 0
    hold_days = np.mean([t["hold_days"] for t in trades]) if trades else 0

    eq = pd.Series(dpnl).cumsum()
    max_dd = ((eq - eq.cummax()) / (eq.cummax() + 1)).min() * 100

    years = (dates[-1] - dates[0]).days / 365.25
    span = span_cap(pa[-252:].mean(), pb[-252:].mean(), lot_a, lot_b, n_a, n_b)
    cagr = ((net_pnl / span + 1) ** (1 / years) - 1) * 100 if net_pnl > 0 else 0

    # Per-year P&L
    df_pnl = pd.Series(dpnl, index=pd.to_datetime([d for d in dates]))
    yearly = df_pnl.groupby(df_pnl.index.year).sum()

    print(f"\n  ── RESULTS ──────────────────────────────────────────")
    print(f"  Pair:         {name}")
    print(f"  Lots:         {n_a}×{lot_a} {sym_a}  |  {n_b}×{lot_b} {sym_b}")
    print(f"  Lookback:     {lookback} days  (HL={hl:.1f}d)")
    print(f"  Params:       entry_z={ez}  stop_z={sz}  annual_stop=₹{annual_stop:,}")
    print(f"  Trades:       {len(trades)}  (win {win_rate:.0f}%  avg win ₹{avg_win:,.0f}  avg loss ₹{avg_loss2:,.0f})")
    print(f"  Avg hold:     {hold_days:.0f} days")
    print(f"  Net P&L:      ₹{net_pnl:>12,.0f}")
    print(f"  SPAN margin:  ₹{span:>12,.0f}")
    print(f"  CAGR on SPAN: {cagr:.2f}%")
    print(f"  Sharpe:       {best['sharpe']:.3f}")
    print(f"  Max DD:       {max_dd:.2f}%")
    print(f"\n  Yearly P&L:")
    for yr, pnl in yearly.items():
        bar = "+" if pnl > 0 else "-"
        print(f"    {yr}: ₹{pnl:>10,.0f}  {bar*max(1, int(abs(pnl)/20000))}")

    verdict = "✅ BACKTEST PASS" if best["sharpe"] > 0.4 and net_pnl > 0 else "❌ BACKTEST FAIL"
    print(f"\n  Verdict: {verdict}")
    print(f"  {'='*60}")


if __name__ == "__main__":
    for name, sym_a, lot_a, sym_b, lot_b in CANDIDATES:
        calibrate(name, sym_a, lot_a, sym_b, lot_b)
