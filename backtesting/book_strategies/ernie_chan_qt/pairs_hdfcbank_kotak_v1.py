# ============================================================
# backtesting/book_strategies/ernie_chan_qt/pairs_hdfcbank_kotak_v1.py
#
# Pairs Trading — HDFCBANK vs KOTAKBANK (V1)
# Source: Ernie Chan, "Quantitative Trading" (2008), Chapter 7
#
# Structure:
#   Signal  : rolling z-score of cointegration spread
#   Entry   : |z| > ENTRY_Z  (spread stretched)
#   Exit    : |z| < EXIT_Z   (spread reverted)
#   Stop    : |z| > STOP_Z   (spread blowing out — cut)
#   Holding : overnight via futures (both legs)
#   Size    : futures lot sizes — HDFC=550, KOTAK=400
#
# Cost structure (futures):
#   Brokerage : 0.03% per leg (both sides)
#   STT       : 0.01% on sell side only (futures rate)
#   Round trip: ~0.07% (better than equity intraday 0.085%)
#
# Usage:
#   G:\fyers_data_pipeline\.venv\Scripts\python.exe
#       backtesting\book_strategies\ernie_chan_qt\pairs_hdfcbank_kotak_v1.py
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
SYMBOL_A = "NSE:HDFCBANK-EQ"
SYMBOL_B = "NSE:KOTAKBANK-EQ"

# Futures lot sizes (NSE F&O as of 2024-25)
LOT_A = 550    # HDFCBANK shares per lot
LOT_B = 400    # KOTAKBANK shares per lot
N_LOTS = 1     # lots of each leg per trade

# Rolling estimation window (trading days)
LOOKBACK = 63    # 3 months for beta + spread mean/std (252 wasted entire first year)

# Signal thresholds
ENTRY_Z = 1.5    # enter when |z-score| exceeds this
EXIT_Z  = 0.5    # exit when |z-score| falls below this
STOP_Z  = 3.0    # emergency stop — spread blowing out

TOTAL_CAPITAL = 1_000_000   # ₹10 lakh

# Futures transaction costs
BROKERAGE_ONEWAY = 0.0003   # 0.03% per leg
STT_FUTURES_SELL = 0.0001   # 0.01% on sell side (futures rate)

# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT PATHS
# ─────────────────────────────────────────────────────────────────────────────
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
EQUITY_CURVE_PATH = RESULTS_DIR / "equity_curve_pairs_v1.png"
TRADES_PATH       = RESULTS_DIR / "trades_pairs_v1.csv"
SPREAD_PLOT_PATH  = RESULTS_DIR / "spread_zscore_pairs_v1.png"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — LOAD & RESAMPLE
# ─────────────────────────────────────────────────────────────────────────────

def load_pair(loader: DataLoader) -> pd.DataFrame:
    """Load HDFCBANK and KOTAKBANK, resample to daily, return aligned DataFrame."""
    print(f"Loading {SYMBOL_A} and {SYMBOL_B}...")
    raw = loader.load_many([SYMBOL_A, SYMBOL_B])

    daily = {}
    for sym, df in raw.items():
        d = resample_ohlcv(df, "1D")
        d.index = d.index.normalize()
        short = sym.split(":")[1].replace("-EQ", "")
        daily[short] = d["close"]

    panel = pd.DataFrame(daily).dropna()
    print(f"Aligned daily data: {len(panel)} days "
          f"({panel.index[0].date()} → {panel.index[-1].date()})\n")
    return panel


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — COINTEGRATION CHECK
# ─────────────────────────────────────────────────────────────────────────────

def adf_test(series: pd.Series) -> dict:
    """
    Augmented Dickey-Fuller test (manual implementation, no statsmodels needed).
    Tests H0: series has a unit root (non-stationary).
    Returns test_stat and a simple verdict.
    """
    s = series.dropna().values
    n = len(s)
    dy = np.diff(s)
    y_lag = s[:-1]

    # OLS: dy = alpha + beta*y_lag
    X = np.column_stack([np.ones(n - 1), y_lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, dy, rcond=None)
        residuals = dy - X @ coeffs
        se = np.sqrt(np.sum(residuals ** 2) / (n - 2) *
                     np.linalg.inv(X.T @ X)[1, 1])
        t_stat = coeffs[1] / se if se > 0 else 0.0
    except Exception:
        t_stat = 0.0

    # Approximate critical values (MacKinnon 1994, no trend, n≥100)
    cv_1pct  = -3.43
    cv_5pct  = -2.86
    cv_10pct = -2.57

    if t_stat < cv_1pct:
        verdict = "STATIONARY at 1% ✅"
    elif t_stat < cv_5pct:
        verdict = "STATIONARY at 5% ✅"
    elif t_stat < cv_10pct:
        verdict = "STATIONARY at 10% ⚠️"
    else:
        verdict = "NON-STATIONARY ❌"

    return {"t_stat": t_stat, "cv_1pct": cv_1pct,
            "cv_5pct": cv_5pct, "verdict": verdict}


def check_cointegration(panel: pd.DataFrame) -> float:
    """
    OLS of HDFCBANK on KOTAKBANK → hedge ratio (beta).
    ADF test on residuals (spread) to confirm cointegration.
    Returns beta.
    """
    price_a = panel["HDFCBANK"].values
    price_b = panel["KOTAKBANK"].values

    # OLS: HDFCBANK = alpha + beta × KOTAKBANK
    X = np.column_stack([np.ones(len(price_b)), price_b])
    coeffs, _, _, _ = np.linalg.lstsq(X, price_a, rcond=None)
    alpha, beta = coeffs

    spread = price_a - beta * price_b

    print("=" * 55)
    print("COINTEGRATION TEST (full sample OLS)")
    print("=" * 55)
    print(f"  HDFCBANK = {alpha:.2f} + {beta:.4f} × KOTAKBANK")
    print(f"  Hedge ratio (beta) : {beta:.4f}")
    print(f"  Meaning : 1 share HDFC ≈ {beta:.2f} shares KOTAK\n")

    # ADF on HDFCBANK prices individually
    adf_a = adf_test(panel["HDFCBANK"])
    adf_b = adf_test(panel["KOTAKBANK"])
    adf_s = adf_test(pd.Series(spread))

    print(f"  ADF — HDFCBANK prices : t={adf_a['t_stat']:.3f}  →  {adf_a['verdict']}")
    print(f"  ADF — KOTAKBANK prices: t={adf_b['t_stat']:.3f}  →  {adf_b['verdict']}")
    print(f"  ADF — Spread (residual): t={adf_s['t_stat']:.3f}  →  {adf_s['verdict']}")
    print()

    if adf_s["t_stat"] < adf_s["cv_5pct"]:
        print("  ✅ Spread is stationary → pair IS cointegrated (5% level)")
    else:
        print("  ⚠️  Spread may not be stationary → weak or no cointegration")
    print("=" * 55 + "\n")

    return float(beta)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — ROLLING SPREAD + Z-SCORE
# ─────────────────────────────────────────────────────────────────────────────

def compute_rolling_signals(panel: pd.DataFrame) -> pd.DataFrame:
    """
    For each day t (after the lookback window):
      - Estimate beta using OLS on trailing LOOKBACK days
      - Compute spread_t = HDFC_t - beta × KOTAK_t
      - Z-score = (spread_t - rolling_mean) / rolling_std
    Returns DataFrame with columns: HDFCBANK, KOTAKBANK, beta, spread, zscore
    """
    prices_a = panel["HDFCBANK"].values
    prices_b = panel["KOTAKBANK"].values
    n        = len(panel)

    betas   = np.full(n, np.nan)
    spreads = np.full(n, np.nan)
    zscores = np.full(n, np.nan)

    for t in range(LOOKBACK, n):
        window_a = prices_a[t - LOOKBACK: t]
        window_b = prices_b[t - LOOKBACK: t]

        # Rolling OLS for hedge ratio
        X = np.column_stack([np.ones(LOOKBACK), window_b])
        coeffs, _, _, _ = np.linalg.lstsq(X, window_a, rcond=None)
        _, beta = coeffs

        # Rolling spread history (to compute mean/std)
        spread_window = window_a - beta * window_b

        betas[t]   = beta
        spreads[t] = prices_a[t] - beta * prices_b[t]
        mu         = spread_window.mean()
        sigma      = spread_window.std()
        zscores[t] = (spreads[t] - mu) / sigma if sigma > 0 else 0.0

    result = panel.copy()
    result["beta"]   = betas
    result["spread"] = spreads
    result["zscore"] = zscores
    return result.dropna(subset=["zscore"])


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — BACKTEST SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def simulate(signals: pd.DataFrame) -> tuple:
    """
    State machine — flat / long-A-short-B / short-A-long-B.
    Entry at close of signal day.
    Exit at close of the day exit condition is met.
    """
    dates    = signals.index.tolist()
    price_a  = signals["HDFCBANK"].values
    price_b  = signals["KOTAKBANK"].values
    zscore   = signals["zscore"].values
    n        = len(signals)

    qty_a = N_LOTS * LOT_A   # shares of HDFCBANK per trade
    qty_b = N_LOTS * LOT_B   # shares of KOTAKBANK per trade

    # Margin estimate (informational)
    avg_price_a = float(signals["HDFCBANK"].mean())
    avg_price_b = float(signals["KOTAKBANK"].mean())
    margin_est  = (qty_a * avg_price_a + qty_b * avg_price_b) * 0.15
    print(f"Position size  : {N_LOTS} lot(s) each")
    print(f"HDFCBANK qty   : {qty_a} shares  (~₹{qty_a*avg_price_a/1e5:.1f}L notional)")
    print(f"KOTAKBANK qty  : {qty_b} shares  (~₹{qty_b*avg_price_b/1e5:.1f}L notional)")
    print(f"Margin needed  : ~₹{margin_est/1e5:.1f}L  (estimated at 15%)")
    print(f"Capital        : ₹{TOTAL_CAPITAL/1e5:.0f}L\n")

    # ── State machine ─────────────────────────────────────────────────────────
    position   = 0    # 0=flat, 1=longA-shortB, -1=shortA-longB
    entry_idx  = None
    entry_pa   = entry_pb = 0.0

    trade_records  = []
    daily_pnl      = pd.Series(0.0, index=signals.index)

    for t in range(n):
        z  = zscore[t]
        pa = price_a[t]
        pb = price_b[t]

        # ── Check exit conditions first (if in a position) ────────────────────
        if position != 0:
            should_exit = False
            exit_reason = ""

            if abs(z) < EXIT_Z:
                should_exit = True
                exit_reason = "reversion"
            elif abs(z) > STOP_Z:
                should_exit = True
                exit_reason = "stop-loss"

            if should_exit:
                # Compute P&L for both legs
                if position == 1:   # long A, short B
                    pnl_a = (pa - entry_pa) * qty_a    # long HDFC
                    pnl_b = (entry_pb - pb) * qty_b    # short KOTAK
                    # Costs: bought A at entry (no STT buy), sell A at exit (STT sell)
                    #        sold B at entry (STT sell), buy B at exit (no STT buy)
                    cost = (entry_pa * qty_a * BROKERAGE_ONEWAY +   # brokerage entry A
                            pa      * qty_a * BROKERAGE_ONEWAY +   # brokerage exit A
                            pa      * qty_a * STT_FUTURES_SELL +   # STT sell A (exit)
                            entry_pb * qty_b * BROKERAGE_ONEWAY +  # brokerage entry B
                            pb       * qty_b * BROKERAGE_ONEWAY +  # brokerage exit B
                            entry_pb * qty_b * STT_FUTURES_SELL)   # STT sell B (entry)
                else:               # short A, long B
                    pnl_a = (entry_pa - pa) * qty_a    # short HDFC
                    pnl_b = (pb - entry_pb) * qty_b    # long KOTAK
                    cost  = (entry_pa * qty_a * BROKERAGE_ONEWAY +
                             pa      * qty_a * BROKERAGE_ONEWAY +
                             entry_pa * qty_a * STT_FUTURES_SELL +  # STT sell A (entry)
                             entry_pb * qty_b * BROKERAGE_ONEWAY +
                             pb       * qty_b * BROKERAGE_ONEWAY +
                             pb       * qty_b * STT_FUTURES_SELL)   # STT sell B (exit)

                gross_pnl = pnl_a + pnl_b
                net_pnl   = gross_pnl - cost
                hold_days = t - entry_idx

                daily_pnl.iloc[t] += net_pnl

                trade_records.append({
                    "entry_date":  dates[entry_idx].date(),
                    "exit_date":   dates[t].date(),
                    "hold_days":   hold_days,
                    "direction":   "LongHDFC-ShortKOTAK" if position == 1
                                   else "ShortHDFC-LongKOTAK",
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

        # ── Check entry conditions (only if flat) ─────────────────────────────
        if position == 0:
            if z > ENTRY_Z:
                # HDFC expensive vs KOTAK → short HDFC, long KOTAK
                position  = -1
                entry_idx = t
                entry_pa  = pa
                entry_pb  = pb
            elif z < -ENTRY_Z:
                # HDFC cheap vs KOTAK → long HDFC, short KOTAK
                position  = 1
                entry_idx = t
                entry_pa  = pa
                entry_pb  = pb

    # Close any open position at last available price
    if position != 0:
        t  = n - 1
        pa = price_a[t]
        pb = price_b[t]
        if position == 1:
            pnl_a = (pa - entry_pa) * qty_a
            pnl_b = (entry_pb - pb) * qty_b
            cost  = (entry_pa * qty_a + pa * qty_a) * BROKERAGE_ONEWAY + \
                     pa * qty_a * STT_FUTURES_SELL + \
                    (entry_pb * qty_b + pb * qty_b) * BROKERAGE_ONEWAY + \
                     entry_pb * qty_b * STT_FUTURES_SELL
        else:
            pnl_a = (entry_pa - pa) * qty_a
            pnl_b = (pb - entry_pb) * qty_b
            cost  = (entry_pa * qty_a + pa * qty_a) * BROKERAGE_ONEWAY + \
                     entry_pa * qty_a * STT_FUTURES_SELL + \
                    (entry_pb * qty_b + pb * qty_b) * BROKERAGE_ONEWAY + \
                     pb * qty_b * STT_FUTURES_SELL
        net_pnl = pnl_a + pnl_b - cost
        daily_pnl.iloc[t] += net_pnl
        trade_records.append({
            "entry_date":  dates[entry_idx].date(),
            "exit_date":   dates[t].date(),
            "hold_days":   t - entry_idx,
            "direction":   "LongHDFC-ShortKOTAK" if position == 1
                           else "ShortHDFC-LongKOTAK",
            "entry_z":     round(zscore[entry_idx], 3),
            "exit_z":      round(zscore[t], 3),
            "exit_reason": "end-of-data",
            "entry_pa": round(entry_pa,2), "exit_pa": round(pa,2),
            "entry_pb": round(entry_pb,2), "exit_pb": round(pb,2),
            "pnl_a": round(pnl_a,2), "pnl_b": round(pnl_b,2),
            "cost": round(cost,2), "net_pnl": round(net_pnl,2),
        })

    trade_df = pd.DataFrame(trade_records)
    return daily_pnl, trade_df


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(daily_pnl: pd.Series, trade_df: pd.DataFrame) -> tuple:
    equity    = TOTAL_CAPITAL + daily_pnl.cumsum()
    daily_ret = daily_pnl / TOTAL_CAPITAL

    mean_ret = daily_ret.mean()
    std_ret  = daily_ret.std()
    sharpe   = (mean_ret / std_ret) * np.sqrt(252) if std_ret > 0 else 0.0

    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max * 100
    max_dd_pct  = drawdown.min()

    max_dd_days = cur = 0
    for in_dd in (drawdown < 0):
        cur = cur + 1 if in_dd else 0
        max_dd_days = max(max_dd_days, cur)

    total_trades    = len(trade_df)
    winning_trades  = (trade_df["net_pnl"] > 0).sum() if total_trades else 0
    win_rate        = winning_trades / total_trades * 100 if total_trades else 0.0
    avg_hold        = trade_df["hold_days"].mean() if total_trades else 0.0
    avg_winner      = trade_df.loc[trade_df["net_pnl"] > 0, "net_pnl"].mean() if winning_trades else 0.0
    avg_loser       = trade_df.loc[trade_df["net_pnl"] <= 0, "net_pnl"].mean() if (total_trades - winning_trades) > 0 else 0.0
    stops_hit       = (trade_df["exit_reason"] == "stop-loss").sum() if total_trades else 0
    net_pnl_total   = daily_pnl.sum()

    return equity, drawdown, dict(
        sharpe=sharpe, max_dd_pct=max_dd_pct, max_dd_days=max_dd_days,
        total_trades=total_trades, win_rate=win_rate, net_pnl=net_pnl_total,
        avg_daily=daily_pnl.mean(), avg_hold=avg_hold,
        avg_winner=avg_winner, avg_loser=avg_loser,
        stops_hit=stops_hit, total_days=len(daily_pnl),
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

def print_results(m: dict):
    print(f"\n{'=' * 50}")
    print("=== PAIRS V1 RESULTS — HDFCBANK / KOTAKBANK ===")
    print(f"{'=' * 50}")
    print(f"Sharpe Ratio      : {m['sharpe']:.3f}")
    print(f"Max Drawdown %    : {m['max_dd_pct']:.2f}%")
    print(f"Max DD Duration   : {m['max_dd_days']} days")
    print(f"Total Trades      : {m['total_trades']}")
    print(f"Win Rate %        : {m['win_rate']:.1f}%")
    print(f"Avg Hold (days)   : {m['avg_hold']:.1f}")
    print(f"Avg Winner        : ₹{m['avg_winner']:,.0f}")
    print(f"Avg Loser         : ₹{m['avg_loser']:,.0f}")
    print(f"Stop-losses hit   : {m['stops_hit']}")
    print(f"Net P&L           : ₹{m['net_pnl']:,.0f}")
    print(f"Avg Daily P&L     : ₹{m['avg_daily']:,.2f}")
    print(f"Total Days Tested : {m['total_days']}")
    print(f"{'=' * 50}")


def save_spread_plot(signals: pd.DataFrame):
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1.5, 1.5]})

    dates = signals.index

    # ── Price panel ───────────────────────────────────────────────────────────
    ax1 = axes[0]
    ax1_r = ax1.twinx()
    ax1.plot(dates, signals["HDFCBANK"],  color="#1565C0", linewidth=1.2, label="HDFCBANK (L)")
    ax1_r.plot(dates, signals["KOTAKBANK"], color="#E65100", linewidth=1.2,
               linestyle="--", label="KOTAKBANK (R)")
    ax1.set_ylabel("HDFCBANK ₹", fontsize=9, color="#1565C0")
    ax1_r.set_ylabel("KOTAKBANK ₹", fontsize=9, color="#E65100")
    ax1.set_title("HDFCBANK vs KOTAKBANK — Pairs Trading V1\n"
                  f"Lookback={LOOKBACK}d | Entry|z|>{ENTRY_Z} | Exit|z|<{EXIT_Z} | Stop|z|>{STOP_Z}",
                  fontsize=11, fontweight="bold")
    lines1, lbl1 = ax1.get_legend_handles_labels()
    lines2, lbl2 = ax1_r.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, lbl1 + lbl2, loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.2)

    # ── Spread panel ──────────────────────────────────────────────────────────
    ax2 = axes[1]
    ax2.plot(dates, signals["spread"], color="#4A148C", linewidth=1.0, label="Spread")
    ax2.axhline(0, color="#9E9E9E", linestyle="--", linewidth=0.7)
    ax2.set_ylabel("Spread ₹", fontsize=9)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.2)

    # ── Z-score panel ─────────────────────────────────────────────────────────
    ax3 = axes[2]
    ax3.plot(dates, signals["zscore"], color="#2E7D32", linewidth=1.0, label="Z-score")
    ax3.axhline(0,        color="#9E9E9E", linestyle="--", linewidth=0.7)
    ax3.axhline(ENTRY_Z,  color="#F44336", linestyle="--", linewidth=0.9,
                label=f"Entry ±{ENTRY_Z}")
    ax3.axhline(-ENTRY_Z, color="#F44336", linestyle="--", linewidth=0.9)
    ax3.axhline(EXIT_Z,   color="#66BB6A", linestyle=":",  linewidth=0.9,
                label=f"Exit ±{EXIT_Z}")
    ax3.axhline(-EXIT_Z,  color="#66BB6A", linestyle=":",  linewidth=0.9)
    ax3.axhline(STOP_Z,   color="#000000", linestyle=":",  linewidth=0.9,
                label=f"Stop ±{STOP_Z}")
    ax3.axhline(-STOP_Z,  color="#000000", linestyle=":",  linewidth=0.9)
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
    print(f"Spread / Z-score plot → {SPREAD_PLOT_PATH}")


def save_equity_plot(daily_pnl: pd.Series, equity: pd.Series,
                     drawdown: pd.Series, m: dict):
    fig, axes = plt.subplots(2, 1, figsize=(14, 7),
                             gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    ax1, ax2 = axes
    dates = pd.to_datetime(daily_pnl.index)

    ax1.plot(dates, equity, color="#1565C0", linewidth=1.5)
    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", linestyle="--",
                linewidth=0.8, alpha=0.7, label="Starting Capital")
    ax1.set_title("Pairs Trading V1 — Equity Curve  (HDFCBANK / KOTAKBANK)",
                  fontsize=12, fontweight="bold")
    ax1.set_ylabel("Portfolio Value (₹)", fontsize=10)
    ax1.yaxis.set_major_formatter(
        plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.2f}M"))
    stats_text = (f"Sharpe: {m['sharpe']:.2f}  |  Max DD: {m['max_dd_pct']:.1f}%  |  "
                  f"Trades: {m['total_trades']}  |  Win: {m['win_rate']:.0f}%  |  "
                  f"Net P&L: ₹{m['net_pnl']:,.0f}")
    ax1.text(0.01, 0.97, stats_text, transform=ax1.transAxes, fontsize=8.5,
             va="top", bbox=dict(boxstyle="round,pad=0.4",
                                  facecolor="#FFF9C4", alpha=0.85))
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
    print(f"Equity curve plot  → {EQUITY_CURVE_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Pairs Trading V1 — HDFCBANK vs KOTAKBANK")
    print(f"HDFCBANK lot: {LOT_A} shares | KOTAKBANK lot: {LOT_B} shares")
    print(f"Lookback: {LOOKBACK} days | Entry Z: ±{ENTRY_Z} | Exit Z: ±{EXIT_Z} | Stop Z: ±{STOP_Z}")
    print(f"Costs: {BROKERAGE_ONEWAY*100:.3f}% brokerage/leg + "
          f"{STT_FUTURES_SELL*100:.3f}% STT (futures rate)")
    print("=" * 60 + "\n")

    loader = DataLoader()
    panel  = load_pair(loader)

    # Step 2: Cointegration check on full sample
    check_cointegration(panel)

    # Step 3: Rolling signals
    print("Computing rolling hedge ratio and z-score...")
    signals = compute_rolling_signals(panel)
    live_start = signals.index[0].date()
    print(f"Live trading starts from: {live_start}  "
          f"(after {LOOKBACK}-day warmup)\n")

    # Step 4: Simulate
    print("Running simulation...")
    daily_pnl, trade_df = simulate(signals)

    if trade_df.empty:
        print("No trades generated — try reducing ENTRY_Z threshold.")
        return

    # Step 5: Metrics
    equity, drawdown, m = compute_metrics(daily_pnl, trade_df)

    # Step 6: Output
    print_results(m)

    # Trade log
    if not trade_df.empty:
        print(f"\n--- All Trades ---")
        display_cols = ["entry_date", "exit_date", "hold_days", "direction",
                        "entry_z", "exit_z", "exit_reason", "net_pnl"]
        print(trade_df[display_cols].to_string(index=False))

    trade_df.to_csv(TRADES_PATH, index=False)
    print(f"\nTrade log CSV → {TRADES_PATH}")

    save_spread_plot(signals)
    save_equity_plot(daily_pnl, equity, drawdown, m)

    print("\nDone.")


if __name__ == "__main__":
    main()
