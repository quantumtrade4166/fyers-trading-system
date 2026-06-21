# ============================================================
# backtesting/book_strategies/ernie_chan_qt/pairs_tcs_infy_v1.py
#
# Pairs Trading — TCS vs INFOSYS (V1)
# Source: Ernie Chan, "Quantitative Trading" (2008), Chapter 7
#
# Cointegration test is run FIRST. If the pair fails, the
# backtest is skipped with a clear reason printed.
#
# Cointegration logic (Engle-Granger two-step):
#   Step 1 — Confirm both stocks are I(1): ADF on prices
#             (we WANT non-stationary here)
#   Step 2 — OLS regression → compute spread (residuals)
#   Step 3 — ADF on spread: must be stationary (I(0))
#   Step 4 — Half-life: how fast does the spread mean-revert?
#             half_life = -log(2) / log(1 + phi)
#             where phi is from AR(1) fit on spread changes
#
# Cost structure (futures, overnight):
#   Brokerage  : 0.03% per leg
#   STT futures: 0.01% on sell side
#   Round trip : ~0.07%
#
# Usage:
#   G:\fyers_data_pipeline\.venv\Scripts\python.exe
#       backtesting\book_strategies\ernie_chan_qt\pairs_tcs_infy_v1.py
# ============================================================

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

# ─────────────────────────────────────────────────────────────────────────────
# PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL_A = "NSE:TCS-EQ"
SYMBOL_B = "NSE:INFY-EQ"
NAME_A   = "TCS"
NAME_B   = "INFY"

# NSE F&O lot sizes (as of 2024-25)
LOT_A  = 150    # TCS   shares per lot
LOT_B  = 400    # INFOSYS shares per lot
N_LOTS = 1      # lots of each leg per trade

# Rolling window
LOOKBACK = 63   # 3-month rolling window for beta + spread stats

# Signal thresholds
ENTRY_Z = 2.5
EXIT_Z  = 0.5
STOP_Z  = 4.0

TOTAL_CAPITAL    = 1_000_000   # ₹10 lakh
BROKERAGE_ONEWAY = 0.0003
STT_FUTURES_SELL = 0.0001

# Cointegration — minimum requirements to proceed
MIN_HALFLIFE_DAYS  = 1    # spread must revert faster than this many days (lower bound)
MAX_HALFLIFE_DAYS  = 30   # spread must revert within this many days (upper bound)
ADF_PVALUE_CUTOFF  = 0.10 # spread ADF must be significant at this level

# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT PATHS
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_DIR       = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
EQUITY_CURVE_PATH = RESULTS_DIR / "equity_curve_pairs_tcs_infy_v1.png"
TRADES_PATH       = RESULTS_DIR / "trades_pairs_tcs_infy_v1.csv"
SPREAD_PLOT_PATH  = RESULTS_DIR / "spread_zscore_tcs_infy_v1.png"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — LOAD DATA
# ─────────────────────────────────────────────────────────────────────────────

def load_pair(loader: DataLoader) -> pd.DataFrame:
    print(f"Loading {SYMBOL_A} and {SYMBOL_B}...")
    raw = loader.load_many([SYMBOL_A, SYMBOL_B])

    daily = {}
    for sym, df in raw.items():
        d = resample_ohlcv(df, "1D")
        d.index = d.index.normalize()
        short = sym.split(":")[1].replace("-EQ", "")
        daily[short] = d["close"]

    panel = pd.DataFrame(daily).dropna()
    print(f"Aligned daily data: {len(panel)} days  "
          f"({panel.index[0].date()} → {panel.index[-1].date()})\n")
    return panel


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — COINTEGRATION TEST SUITE
# ─────────────────────────────────────────────────────────────────────────────

def adf_stat(series: pd.Series) -> float:
    """Return ADF t-statistic (manual, no external dependency)."""
    s = series.dropna().values
    n = len(s)
    if n < 10:
        return 0.0
    dy    = np.diff(s)
    y_lag = s[:-1]
    X     = np.column_stack([np.ones(n - 1), y_lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, dy, rcond=None)
        residuals = dy - X @ coeffs
        var        = np.sum(residuals ** 2) / max(n - 3, 1)
        inv_XtX    = np.linalg.inv(X.T @ X)
        se         = np.sqrt(var * inv_XtX[1, 1])
        return float(coeffs[1] / se) if se > 0 else 0.0
    except Exception:
        return 0.0


def adf_verdict(t_stat: float, series_label: str) -> dict:
    """Map t-stat to critical values and return a verdict dict."""
    # MacKinnon (1994) critical values — no constant, no trend
    cv = {"1%": -3.43, "5%": -2.86, "10%": -2.57}

    if t_stat < cv["1%"]:
        sig   = "1%"
        stat  = "STATIONARY ✅"
        pval  = "< 0.01"
    elif t_stat < cv["5%"]:
        sig   = "5%"
        stat  = "STATIONARY ✅"
        pval  = "< 0.05"
    elif t_stat < cv["10%"]:
        sig   = "10%"
        stat  = "STATIONARY ⚠️"
        pval  = "< 0.10"
    else:
        sig   = "—"
        stat  = "NON-STATIONARY ❌"
        pval  = "> 0.10"

    return {"label": series_label, "t_stat": t_stat,
            "sig": sig, "status": stat, "pval": pval,
            "cv_1pct": cv["1%"], "cv_5pct": cv["5%"], "cv_10pct": cv["10%"]}


def compute_halflife(spread: pd.Series) -> float:
    """
    Half-life of mean reversion via AR(1) on spread changes.
    Model: Δspread_t = phi × spread_{t-1} + epsilon
    phi < 0 means mean-reverting.
    half_life = -log(2) / log(1 + phi)
    """
    s   = spread.dropna().values
    dy  = np.diff(s)
    lag = s[:-1]
    X   = np.column_stack([np.ones(len(lag)), lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, dy, rcond=None)
        phi = float(coeffs[1])
        if phi >= 0:
            return np.inf   # not mean-reverting
        return float(-np.log(2) / np.log(1 + phi))
    except Exception:
        return np.inf


def run_cointegration_suite(panel: pd.DataFrame) -> tuple:
    """
    Full Engle-Granger cointegration test.
    Returns (beta, spread, passed, half_life).
    Prints detailed report.
    """
    price_a = panel[NAME_A].values
    price_b = panel[NAME_B].values

    # OLS: TCS = alpha + beta × INFY
    X       = np.column_stack([np.ones(len(price_b)), price_b])
    coeffs, _, _, _ = np.linalg.lstsq(X, price_a, rcond=None)
    alpha, beta     = float(coeffs[0]), float(coeffs[1])
    spread          = pd.Series(price_a - beta * price_b, index=panel.index)

    # ADF tests
    r_a   = adf_verdict(adf_stat(panel[NAME_A]), f"{NAME_A} price")
    r_b   = adf_verdict(adf_stat(panel[NAME_B]), f"{NAME_B} price")
    r_spr = adf_verdict(adf_stat(spread),         "Spread (residual)")

    # Half-life
    hl = compute_halflife(spread)

    # ── Print report ──────────────────────────────────────────────────────────
    sep = "=" * 62
    print(sep)
    print(f"  COINTEGRATION REPORT — {NAME_A} / {NAME_B}")
    print(sep)

    print(f"\n  OLS Regression: {NAME_A} = {alpha:.2f} + {beta:.4f} × {NAME_B}")
    print(f"  Hedge ratio (beta) : {beta:.4f}")
    print(f"  Meaning            : 1 share {NAME_A} ≈ {beta:.2f} shares {NAME_B}\n")

    print(f"  {'Series':<22} {'ADF t-stat':>12} {'p-value':>10} {'Result'}")
    print(f"  {'-'*58}")
    for r in [r_a, r_b, r_spr]:
        print(f"  {r['label']:<22} {r['t_stat']:>12.3f} {r['pval']:>10}   {r['status']}")

    print(f"\n  Critical values (MacKinnon 1994):")
    print(f"    1%  → {r_spr['cv_1pct']:.2f}   5%  → {r_spr['cv_5pct']:.2f}"
          f"   10% → {r_spr['cv_10pct']:.2f}\n")

    if hl == np.inf:
        hl_str = "∞ (not mean-reverting)"
    else:
        hl_str = f"{hl:.1f} trading days"
    print(f"  Half-life of mean reversion: {hl_str}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    # For cointegration we WANT:
    #   prices     → NON-stationary (I(1)) ← expected for stocks, just confirming
    #   spread     → STATIONARY      (I(0)) ← critical requirement
    # Plus half-life must be in a tradeable range
    spread_stationary = r_spr["t_stat"] < r_spr["cv_10pct"]   # 10% threshold
    hl_ok             = (hl != np.inf) and (MIN_HALFLIFE_DAYS < hl < MAX_HALFLIFE_DAYS)

    print(f"\n  {'─'*58}")
    print(f"  CHECKLIST:")
    tick = lambda ok: "✅" if ok else "❌"
    print(f"    {tick(r_a['t_stat'] > r_a['cv_10pct'])}  {NAME_A} price is non-stationary (I(1))"
          f"  [t={r_a['t_stat']:.3f}]")
    print(f"    {tick(r_b['t_stat'] > r_b['cv_10pct'])}  {NAME_B} price is non-stationary (I(1))"
          f"  [t={r_b['t_stat']:.3f}]")
    print(f"    {tick(spread_stationary)}  Spread is stationary (I(0))"
          f"              [t={r_spr['t_stat']:.3f}, need < {r_spr['cv_10pct']}]")
    if hl == np.inf:
        print(f"    ❌  Half-life is in tradeable range (1–20 days)       [∞]")
    else:
        print(f"    {tick(hl_ok)}  Half-life is in tradeable range (1–20 days)"
              f"       [{hl:.1f} days]")

    passed = spread_stationary and hl_ok
    verdict_str = "✅ PAIR QUALIFIES FOR COINTEGRATION TRADING" if passed \
                  else "❌ PAIR DOES NOT QUALIFY — backtest not meaningful"
    print(f"\n  VERDICT: {verdict_str}")
    print(sep + "\n")

    return beta, spread, passed, hl


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — ROLLING SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

def compute_rolling_signals(panel: pd.DataFrame) -> pd.DataFrame:
    prices_a = panel[NAME_A].values
    prices_b = panel[NAME_B].values
    n        = len(panel)

    betas   = np.full(n, np.nan)
    spreads = np.full(n, np.nan)
    zscores = np.full(n, np.nan)

    for t in range(LOOKBACK, n):
        wa = prices_a[t - LOOKBACK: t]
        wb = prices_b[t - LOOKBACK: t]
        X  = np.column_stack([np.ones(LOOKBACK), wb])
        coeffs, _, _, _ = np.linalg.lstsq(X, wa, rcond=None)
        _, beta = coeffs

        sw         = wa - beta * wb
        betas[t]   = beta
        spreads[t] = prices_a[t] - beta * prices_b[t]
        mu, sigma  = sw.mean(), sw.std()
        zscores[t] = (spreads[t] - mu) / sigma if sigma > 0 else 0.0

    result         = panel.copy()
    result["beta"] = betas
    result["spread"]  = spreads
    result["zscore"]  = zscores
    return result.dropna(subset=["zscore"])


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def simulate(signals: pd.DataFrame) -> tuple:
    dates    = signals.index.tolist()
    price_a  = signals[NAME_A].values
    price_b  = signals[NAME_B].values
    zscore   = signals["zscore"].values
    n        = len(signals)

    qty_a = N_LOTS * LOT_A
    qty_b = N_LOTS * LOT_B

    avg_pa = float(signals[NAME_A].mean())
    avg_pb = float(signals[NAME_B].mean())
    margin = (qty_a * avg_pa + qty_b * avg_pb) * 0.15

    print(f"  {NAME_A} qty   : {qty_a} shares  (~₹{qty_a*avg_pa/1e5:.1f}L notional)")
    print(f"  {NAME_B} qty   : {qty_b} shares  (~₹{qty_b*avg_pb/1e5:.1f}L notional)")
    print(f"  Margin needed  : ~₹{margin/1e5:.1f}L  (estimated at 15%)")
    print(f"  Capital        : ₹{TOTAL_CAPITAL/1e5:.0f}L\n")

    position   = 0
    entry_idx  = None
    entry_pa   = entry_pb = 0.0
    cooldown   = 0          # bars to wait after a stop before re-entering

    trade_records = []
    daily_pnl     = pd.Series(0.0, index=signals.index)

    for t in range(n):
        z  = zscore[t]
        pa = price_a[t]
        pb = price_b[t]

        if cooldown > 0:
            cooldown -= 1

        # ── Exit ──────────────────────────────────────────────────────────────
        if position != 0:
            should_exit = False
            exit_reason = ""

            if abs(z) < EXIT_Z:
                should_exit = True
                exit_reason = "reversion"
            elif abs(z) > STOP_Z:
                should_exit = True
                exit_reason = "stop-loss"
                cooldown    = 3   # 3-day cooldown after stop

            if should_exit:
                if position == 1:    # long A, short B
                    pnl_a = (pa - entry_pa) * qty_a
                    pnl_b = (entry_pb - pb) * qty_b
                    cost  = ((entry_pa * qty_a + pa * qty_a) * BROKERAGE_ONEWAY
                             + pa * qty_a * STT_FUTURES_SELL
                             + (entry_pb * qty_b + pb * qty_b) * BROKERAGE_ONEWAY
                             + entry_pb * qty_b * STT_FUTURES_SELL)
                else:                # short A, long B
                    pnl_a = (entry_pa - pa) * qty_a
                    pnl_b = (pb - entry_pb) * qty_b
                    cost  = ((entry_pa * qty_a + pa * qty_a) * BROKERAGE_ONEWAY
                             + entry_pa * qty_a * STT_FUTURES_SELL
                             + (entry_pb * qty_b + pb * qty_b) * BROKERAGE_ONEWAY
                             + pb * qty_b * STT_FUTURES_SELL)

                net_pnl = pnl_a + pnl_b - cost
                daily_pnl.iloc[t] += net_pnl

                trade_records.append({
                    "entry_date":  dates[entry_idx].date(),
                    "exit_date":   dates[t].date(),
                    "hold_days":   t - entry_idx,
                    "direction":   f"Long{NAME_A}-Short{NAME_B}" if position == 1
                                   else f"Short{NAME_A}-Long{NAME_B}",
                    "entry_z":     round(zscore[entry_idx], 3),
                    "exit_z":      round(z, 3),
                    "exit_reason": exit_reason,
                    "entry_pa":    round(entry_pa, 2),
                    "exit_pa":     round(pa, 2),
                    "entry_pb":    round(entry_pb, 2),
                    "exit_pb":     round(pb, 2),
                    "pnl_a":       round(pnl_a, 2),
                    "pnl_b":       round(pnl_b, 2),
                    "cost":        round(cost, 2),
                    "net_pnl":     round(net_pnl, 2),
                })
                position  = 0
                entry_idx = None

        # ── Entry (flat + no cooldown) ─────────────────────────────────────────
        if position == 0 and cooldown == 0:
            if z > ENTRY_Z:
                position, entry_idx, entry_pa, entry_pb = -1, t, pa, pb
            elif z < -ENTRY_Z:
                position, entry_idx, entry_pa, entry_pb =  1, t, pa, pb

    # Close any open position at last price
    if position != 0 and entry_idx is not None:
        t  = n - 1
        pa = price_a[t]
        pb = price_b[t]
        if position == 1:
            pnl_a = (pa - entry_pa) * qty_a
            pnl_b = (entry_pb - pb) * qty_b
            cost  = ((entry_pa * qty_a + pa * qty_a) * BROKERAGE_ONEWAY
                     + pa * qty_a * STT_FUTURES_SELL
                     + (entry_pb * qty_b + pb * qty_b) * BROKERAGE_ONEWAY
                     + entry_pb * qty_b * STT_FUTURES_SELL)
        else:
            pnl_a = (entry_pa - pa) * qty_a
            pnl_b = (pb - entry_pb) * qty_b
            cost  = ((entry_pa * qty_a + pa * qty_a) * BROKERAGE_ONEWAY
                     + entry_pa * qty_a * STT_FUTURES_SELL
                     + (entry_pb * qty_b + pb * qty_b) * BROKERAGE_ONEWAY
                     + pb * qty_b * STT_FUTURES_SELL)

        net_pnl = pnl_a + pnl_b - cost
        daily_pnl.iloc[t] += net_pnl
        trade_records.append({
            "entry_date": dates[entry_idx].date(),
            "exit_date":  dates[t].date(),
            "hold_days":  t - entry_idx,
            "direction":  f"Long{NAME_A}-Short{NAME_B}" if position == 1
                          else f"Short{NAME_A}-Long{NAME_B}",
            "entry_z": round(zscore[entry_idx], 3), "exit_z": round(zscore[t], 3),
            "exit_reason": "end-of-data",
            "entry_pa": round(entry_pa,2), "exit_pa": round(pa,2),
            "entry_pb": round(entry_pb,2), "exit_pb": round(pb,2),
            "pnl_a": round(pnl_a,2), "pnl_b": round(pnl_b,2),
            "cost": round(cost,2), "net_pnl": round(net_pnl,2),
        })

    return daily_pnl, pd.DataFrame(trade_records)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(daily_pnl: pd.Series, trade_df: pd.DataFrame) -> tuple:
    equity      = TOTAL_CAPITAL + daily_pnl.cumsum()
    daily_ret   = daily_pnl / TOTAL_CAPITAL
    mean_ret    = daily_ret.mean()
    std_ret     = daily_ret.std()
    sharpe      = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max * 100
    max_dd_pct  = drawdown.min()

    max_dd_days = cur = 0
    for in_dd in (drawdown < 0):
        cur = cur + 1 if in_dd else 0
        max_dd_days = max(max_dd_days, cur)

    n_trades   = len(trade_df)
    n_wins     = (trade_df["net_pnl"] > 0).sum() if n_trades else 0
    win_rate   = n_wins / n_trades * 100 if n_trades else 0.0
    avg_hold   = trade_df["hold_days"].mean() if n_trades else 0.0
    avg_win    = trade_df.loc[trade_df["net_pnl"] > 0, "net_pnl"].mean() if n_wins else 0.0
    avg_loss   = trade_df.loc[trade_df["net_pnl"] <= 0, "net_pnl"].mean() \
                 if (n_trades - n_wins) > 0 else 0.0
    stops      = (trade_df["exit_reason"] == "stop-loss").sum() if n_trades else 0
    pf_num     = trade_df.loc[trade_df["net_pnl"] > 0, "net_pnl"].sum()
    pf_den     = abs(trade_df.loc[trade_df["net_pnl"] <= 0, "net_pnl"].sum())
    pf         = pf_num / pf_den if pf_den > 0 else np.inf

    return equity, drawdown, dict(
        sharpe=sharpe, max_dd_pct=max_dd_pct, max_dd_days=max_dd_days,
        total_trades=n_trades, win_rate=win_rate, net_pnl=daily_pnl.sum(),
        avg_daily=daily_pnl.mean(), avg_hold=avg_hold,
        avg_winner=avg_win, avg_loser=avg_loss,
        stops_hit=stops, profit_factor=pf,
        total_days=len(daily_pnl),
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — PRINT & SAVE
# ─────────────────────────────────────────────────────────────────────────────

def print_results(m: dict):
    print(f"\n{'=' * 55}")
    print(f"  BACKTEST RESULTS — {NAME_A} / {NAME_B}")
    print(f"{'=' * 55}")
    print(f"  Sharpe Ratio      : {m['sharpe']:.3f}")
    print(f"  Max Drawdown %    : {m['max_dd_pct']:.2f}%")
    print(f"  Max DD Duration   : {m['max_dd_days']} days")
    print(f"  Total Trades      : {m['total_trades']}")
    print(f"  Win Rate %        : {m['win_rate']:.1f}%")
    print(f"  Profit Factor     : {m['profit_factor']:.2f}")
    print(f"  Avg Hold (days)   : {m['avg_hold']:.1f}")
    print(f"  Avg Winner        : ₹{m['avg_winner']:,.0f}")
    print(f"  Avg Loser         : ₹{m['avg_loser']:,.0f}")
    print(f"  Stop-losses hit   : {m['stops_hit']} / {m['total_trades']}")
    print(f"  Net P&L           : ₹{m['net_pnl']:,.0f}")
    print(f"  Avg Daily P&L     : ₹{m['avg_daily']:,.2f}")
    print(f"  Total Days Tested : {m['total_days']}")
    print(f"{'=' * 55}")


def save_spread_plot(signals: pd.DataFrame, full_spread: pd.Series):
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1.5, 1.5]})
    dates = signals.index

    # Prices
    ax1 = axes[0]
    ax1r = ax1.twinx()
    ax1.plot(dates,  signals[NAME_A], color="#1565C0", lw=1.2, label=f"{NAME_A} (L)")
    ax1r.plot(dates, signals[NAME_B], color="#E65100", lw=1.2, ls="--",
              label=f"{NAME_B} (R)")
    ax1.set_ylabel(f"{NAME_A} ₹", fontsize=9, color="#1565C0")
    ax1r.set_ylabel(f"{NAME_B} ₹", fontsize=9, color="#E65100")
    ax1.set_title(
        f"{NAME_A} vs {NAME_B} — Pairs Trading V1\n"
        f"Lookback={LOOKBACK}d | Entry|z|>{ENTRY_Z} | Exit|z|<{EXIT_Z} | Stop|z|>{STOP_Z}",
        fontsize=11, fontweight="bold")
    l1, lb1 = ax1.get_legend_handles_labels()
    l2, lb2 = ax1r.get_legend_handles_labels()
    ax1.legend(l1 + l2, lb1 + lb2, loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.2)

    # Spread
    ax2 = axes[1]
    ax2.plot(dates, signals["spread"], color="#4A148C", lw=1.0, label="Rolling Spread")
    ax2.axhline(0, color="#9E9E9E", ls="--", lw=0.7)
    ax2.set_ylabel("Spread ₹", fontsize=9)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.2)

    # Z-score
    ax3 = axes[2]
    ax3.plot(dates, signals["zscore"], color="#2E7D32", lw=1.0, label="Z-score")
    for level, color, style, lbl in [
        (ENTRY_Z,  "#F44336", "--", f"Entry ±{ENTRY_Z}"),
        (-ENTRY_Z, "#F44336", "--", None),
        (EXIT_Z,   "#66BB6A", ":",  f"Exit ±{EXIT_Z}"),
        (-EXIT_Z,  "#66BB6A", ":",  None),
        (STOP_Z,   "#000000", ":",  f"Stop ±{STOP_Z}"),
        (-STOP_Z,  "#000000", ":",  None),
    ]:
        ax3.axhline(level, color=color, ls=style, lw=0.9,
                    label=lbl if lbl else "_nolegend_")
    ax3.set_ylabel("Z-score", fontsize=9)
    ax3.set_xlabel("Date", fontsize=9)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax3.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(True, alpha=0.2)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(SPREAD_PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Spread / Z-score plot → {SPREAD_PLOT_PATH}")


def save_equity_plot(daily_pnl, equity, drawdown, m):
    fig, axes = plt.subplots(2, 1, figsize=(14, 7),
                             gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    ax1, ax2 = axes
    dates = pd.to_datetime(daily_pnl.index)

    color = "#2E7D32" if m["net_pnl"] >= 0 else "#C62828"
    ax1.plot(dates, equity, color=color, lw=1.5)
    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", ls="--", lw=0.8, alpha=0.7,
                label="Starting Capital")
    ax1.set_title(f"Pairs Trading V1 — {NAME_A} / {NAME_B} — Equity Curve",
                  fontsize=12, fontweight="bold")
    ax1.set_ylabel("Portfolio Value (₹)", fontsize=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.3f}M"))
    stats = (f"Sharpe: {m['sharpe']:.2f}  |  Max DD: {m['max_dd_pct']:.1f}%  |  "
             f"Trades: {m['total_trades']}  |  Win: {m['win_rate']:.0f}%  |  "
             f"PF: {m['profit_factor']:.2f}  |  Net: ₹{m['net_pnl']:,.0f}")
    ax1.text(0.01, 0.97, stats, transform=ax1.transAxes, fontsize=8.5, va="top",
             bbox=dict(boxstyle="round,pad=0.4", facecolor="#FFF9C4", alpha=0.85))
    ax1.legend(loc="upper right", fontsize=9)
    ax1.grid(True, alpha=0.25)

    ax2.fill_between(dates, drawdown, 0, color="#E53935", alpha=0.55)
    ax2.set_ylabel("Drawdown %", fontsize=9)
    ax2.set_xlabel("Date", fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax2.grid(True, alpha=0.25)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(EQUITY_CURVE_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Equity curve plot  → {EQUITY_CURVE_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print(f"  Pairs Trading V1 — {NAME_A} vs {NAME_B}")
    print(f"  {NAME_A} lot: {LOT_A} shares  |  {NAME_B} lot: {LOT_B} shares")
    print(f"  Lookback: {LOOKBACK}d  |  Entry ±{ENTRY_Z}  |  Exit ±{EXIT_Z}  |  Stop ±{STOP_Z}")
    print(f"  Costs: {BROKERAGE_ONEWAY*100:.3f}% brokerage/leg + "
          f"{STT_FUTURES_SELL*100:.3f}% STT (futures)")
    print("=" * 62 + "\n")

    loader = DataLoader()
    panel  = load_pair(loader)

    # ── Cointegration test (gate — must pass to proceed) ──────────────────────
    beta, full_spread, coint_passed, half_life = run_cointegration_suite(panel)

    if not coint_passed:
        print("⛔  Cointegration test FAILED.")
        print(f"    Spread is not stationary — {NAME_A}/{NAME_B} is not a valid pair.")
        print("    Backtest skipped. Try a different pair.\n")
        # Still save the spread plot so user can inspect visually
        signals = compute_rolling_signals(panel)
        if not signals.empty:
            save_spread_plot(signals, full_spread)
        return

    print(f"✅  Pair qualifies. Half-life = {half_life:.1f} days → "
          f"using {LOOKBACK}-day rolling window.\n")

    # ── Rolling signals ───────────────────────────────────────────────────────
    print("Computing rolling hedge ratio and z-score...")
    signals = compute_rolling_signals(panel)
    print(f"Live trading starts: {signals.index[0].date()}  "
          f"(after {LOOKBACK}-day warmup)\n")

    # ── Simulate ──────────────────────────────────────────────────────────────
    print("Running simulation...\n")
    daily_pnl, trade_df = simulate(signals)

    if trade_df.empty:
        print("No trades generated — try reducing ENTRY_Z.")
        return

    # ── Metrics ───────────────────────────────────────────────────────────────
    equity, drawdown, m = compute_metrics(daily_pnl, trade_df)
    print_results(m)

    # ── Trade log ─────────────────────────────────────────────────────────────
    display = ["entry_date", "exit_date", "hold_days", "direction",
               "entry_z", "exit_z", "exit_reason", "net_pnl"]
    print(f"\n--- Trade Log ({len(trade_df)} trades) ---")
    print(trade_df[display].to_string(index=False))

    # ── Save outputs ──────────────────────────────────────────────────────────
    trade_df.to_csv(TRADES_PATH, index=False)
    print(f"\n  Trade log CSV      → {TRADES_PATH}")
    save_spread_plot(signals, full_spread)
    save_equity_plot(daily_pnl, equity, drawdown, m)
    print("\nDone.")


if __name__ == "__main__":
    main()
