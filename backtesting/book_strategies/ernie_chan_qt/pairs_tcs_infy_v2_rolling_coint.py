# ============================================================
# pairs_tcs_infy_v2_rolling_coint.py
#
# TCS / INFY Pairs Trading — V2: Rolling Cointegration Gate
#
# KEY DIFFERENCE vs longrun.py (naive version):
#   Every RECHECK_EVERY days, re-test cointegration on the
#   last COINT_WINDOW days. Only allow entries when the pair
#   currently qualifies (spread stationary + half-life ok).
#   Force-exit any open trade if cointegration breaks.
#
# This mimics how real pairs traders continuously monitor
# whether a pair remains cointegrated and pause during
# structural breaks (e.g., HDFCBANK-HDFC merger period).
#
# Data: Yahoo Finance (2015–2024) + Fyers (2024–2026)
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
import matplotlib.patches as mpatches
import yfinance as yf

from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

# ─────────────────────────────────────────────────────────────────────────────
# PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL_A    = "NSE:TCS-EQ"
SYMBOL_B    = "NSE:INFY-EQ"
TICKER_A    = "TCS.NS"
TICKER_B    = "INFY.NS"
NAME_A      = "TCS"
NAME_B      = "INFY"

YF_START    = "2015-01-01"
YF_END      = "2024-05-27"

LOT_A       = 150
LOT_B       = 400
N_LOTS      = 1

# Signal parameters (same as V1)
LOOKBACK    = 63    # standard 3-month lookback
ENTRY_Z     = 1.5
EXIT_Z      = 0.5
STOP_Z      = 3.0

# Rolling cointegration gate (NEW in V2)
COINT_WINDOW   = 126   # days of history used to test cointegration (6 months)
RECHECK_EVERY  = 21    # re-test every 21 trading days (~1 month)
MAX_HL_ROLLING = 90    # max allowed half-life in the rolling window

TOTAL_CAPITAL    = 1_000_000
BROKERAGE_ONEWAY = 0.0003
STT_FUTURES_SELL = 0.0001

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CACHE_DIR   = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CACHE_FILE        = CACHE_DIR / "tcs_infy_daily_2015_2024.parquet"
EQUITY_CURVE_PATH = RESULTS_DIR / "equity_curve_tcs_infy_v2_rolling_coint.png"
TRADES_PATH       = RESULTS_DIR / "trades_tcs_infy_v2_rolling_coint.csv"
SPREAD_PLOT_PATH  = RESULTS_DIR / "spread_zscore_tcs_infy_v2_rolling_coint.png"
REGIME_PLOT_PATH  = RESULTS_DIR / "regime_tcs_infy_v2_rolling_coint.png"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — DATA LOAD & STITCH  (identical to longrun.py)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yahoo(ticker: str, start: str, end: str) -> pd.Series:
    print(f"  Downloading {ticker} from Yahoo Finance ({start} → {end})...")
    df = yf.download(ticker, start=start, end=end,
                     auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"No data returned for {ticker}")
    close = df["Close"].squeeze()
    close.index = pd.to_datetime(close.index).normalize()
    close.name  = ticker.split(".")[0]
    return close.dropna()


def load_yahoo_panel() -> pd.DataFrame:
    if CACHE_FILE.exists():
        print(f"  Loading cached Yahoo data from {CACHE_FILE.name}...")
        panel = pd.read_parquet(CACHE_FILE)
        print(f"  Cached: {len(panel)} days "
              f"({panel.index[0].date()} → {panel.index[-1].date()})")
        return panel
    print("  Cache not found — downloading from Yahoo Finance...")
    tcs  = fetch_yahoo(TICKER_A, YF_START, YF_END)
    infy = fetch_yahoo(TICKER_B, YF_START, YF_END)
    panel = pd.DataFrame({NAME_A: tcs, NAME_B: infy}).dropna()
    panel.to_parquet(CACHE_FILE)
    print(f"  Saved to cache: {CACHE_FILE.name}")
    return panel


def load_fyers_panel(loader: DataLoader) -> pd.DataFrame:
    raw = loader.load_many([SYMBOL_A, SYMBOL_B])
    daily = {}
    for sym, df in raw.items():
        d = resample_ohlcv(df, "1D")
        d.index = d.index.normalize()
        name = sym.split(":")[1].replace("-EQ", "")
        daily[name] = d["close"]
    return pd.DataFrame(daily).dropna()


def load_combined_panel(loader: DataLoader) -> pd.DataFrame:
    print("\n── Loading historical data (Yahoo Finance) ──")
    yf_panel = load_yahoo_panel()

    print("\n── Loading recent data (Fyers 5-min → daily) ──")
    fy_panel = load_fyers_panel(loader)

    cutoff  = pd.Timestamp(YF_END).normalize()
    yf_trim = yf_panel[yf_panel.index <= cutoff]
    fy_trim = fy_panel[fy_panel.index >  cutoff]

    combined = pd.concat([yf_trim, fy_trim]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")].dropna()

    last_yahoo_date  = yf_trim.index[-1]
    first_fyers_date = fy_trim.index[0] if not fy_trim.empty else None

    print(f"\n── Stitch diagnostics (Yahoo ends {last_yahoo_date.date()}, "
          f"Fyers starts {first_fyers_date.date() if first_fyers_date else 'N/A'}) ──")
    before = combined[combined.index == last_yahoo_date]
    after  = combined[combined.index >  last_yahoo_date].head(1)
    if not before.empty and not after.empty:
        for name in [NAME_A, NAME_B]:
            p_before = before[name].values[0]
            p_after  = after[name].values[0]
            gap_pct  = (p_after - p_before) / p_before * 100
            print(f"  {name}: Yahoo close {p_before:.2f}  →  "
                  f"Fyers open {p_after:.2f}  (gap: {gap_pct:+.2f}%)")
    print()
    print(f"── Combined panel ──────────────────────────────────────────")
    print(f"  Total days : {len(combined)}")
    print(f"  Date range : {combined.index[0].date()} → {combined.index[-1].date()}")
    print(f"  Yahoo days : {len(yf_trim)}  ({yf_trim.index[0].date()} → {yf_trim.index[-1].date()})")
    print(f"  Fyers days : {len(fy_trim)}  ({fy_trim.index[0].date()} → {fy_trim.index[-1].date()})")
    print()
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# ADF / HALFLIFE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def adf_stat(series: pd.Series) -> float:
    s = series.dropna().values
    n = len(s)
    if n < 10:
        return 0.0
    dy, lag = np.diff(s), s[:-1]
    X = np.column_stack([np.ones(n - 1), lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, dy, rcond=None)
        resid = dy - X @ coeffs
        var   = np.sum(resid ** 2) / max(n - 3, 1)
        se    = np.sqrt(var * np.linalg.inv(X.T @ X)[1, 1])
        return float(coeffs[1] / se) if se > 0 else 0.0
    except Exception:
        return 0.0


def compute_halflife(spread: np.ndarray) -> float:
    dy  = np.diff(spread)
    lag = spread[:-1]
    X   = np.column_stack([np.ones(len(lag)), lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, dy, rcond=None)
        phi = float(coeffs[1])
        return float(-np.log(2) / np.log(1 + phi)) if phi < 0 else np.inf
    except Exception:
        return np.inf


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — ROLLING COINTEGRATION REGIME  (NEW in V2)
# ─────────────────────────────────────────────────────────────────────────────

def compute_rolling_regime(panel: pd.DataFrame) -> pd.DataFrame:
    """
    Every RECHECK_EVERY days, test cointegration on the last COINT_WINDOW days.
    Returns a DataFrame with columns:
        regime_active : bool  — True = trading allowed
        rolling_hl    : float — half-life in that window
        rolling_adf   : float — ADF t-stat of spread
    """
    pa = panel[NAME_A].values
    pb = panel[NAME_B].values
    n  = len(panel)

    regime_active = np.zeros(n, dtype=bool)
    rolling_hl    = np.full(n, np.nan)
    rolling_adf   = np.full(n, np.nan)

    # MacKinnon 10% critical value for ADF (more permissive for rolling windows)
    ADF_THRESH = -2.57

    last_result   = False   # carry forward the last decision
    last_hl       = np.inf
    last_adf      = 0.0
    last_check    = 0       # index of last recheck

    for t in range(n):
        # Re-check at start of usable data (once COINT_WINDOW days available)
        # and then every RECHECK_EVERY days
        if t >= COINT_WINDOW and (t == COINT_WINDOW or
                                   (t - last_check) >= RECHECK_EVERY):
            wa = pa[t - COINT_WINDOW : t]
            wb = pb[t - COINT_WINDOW : t]

            # OLS: spread = TCS - beta * INFY
            X = np.column_stack([np.ones(COINT_WINDOW), wb])
            coeffs, _, _, _ = np.linalg.lstsq(X, wa, rcond=None)
            beta   = float(coeffs[1])
            spread = wa - beta * wb

            # ADF on spread
            t_stat = adf_stat(pd.Series(spread))
            hl     = compute_halflife(spread)

            spread_ok = t_stat < ADF_THRESH
            hl_ok     = np.isfinite(hl) and 1 < hl < MAX_HL_ROLLING

            last_result = spread_ok and hl_ok
            last_hl     = hl
            last_adf    = t_stat
            last_check  = t

        regime_active[t] = last_result
        rolling_hl[t]    = last_hl
        rolling_adf[t]   = last_adf

    result = panel.copy()
    result["regime_active"] = regime_active
    result["rolling_hl"]    = rolling_hl
    result["rolling_adf"]   = rolling_adf
    return result


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — ROLLING SIGNALS  (same as before)
# ─────────────────────────────────────────────────────────────────────────────

def compute_rolling_signals(panel: pd.DataFrame) -> pd.DataFrame:
    pa, pb = panel[NAME_A].values, panel[NAME_B].values
    n      = len(panel)
    betas   = np.full(n, np.nan)
    spreads = np.full(n, np.nan)
    zscores = np.full(n, np.nan)

    for t in range(LOOKBACK, n):
        wa, wb = pa[t-LOOKBACK:t], pb[t-LOOKBACK:t]
        X = np.column_stack([np.ones(LOOKBACK), wb])
        _, beta = np.linalg.lstsq(X, wa, rcond=None)[0]
        sw         = wa - beta * wb
        betas[t]   = beta
        spreads[t] = pa[t] - beta * pb[t]
        mu, sigma  = sw.mean(), sw.std()
        zscores[t] = (spreads[t] - mu) / sigma if sigma > 0 else 0.0

    result = panel.copy()
    result["beta"]   = betas
    result["spread"] = spreads
    result["zscore"] = zscores
    return result.dropna(subset=["zscore"])


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — SIMULATION  (regime-gated)
# ─────────────────────────────────────────────────────────────────────────────

def simulate(signals: pd.DataFrame) -> tuple:
    dates   = signals.index.tolist()
    pa      = signals[NAME_A].values
    pb      = signals[NAME_B].values
    zs      = signals["zscore"].values
    regime  = signals["regime_active"].values   # bool array
    n       = len(signals)
    qty_a, qty_b = N_LOTS * LOT_A, N_LOTS * LOT_B

    avg_pa, avg_pb = float(signals[NAME_A].mean()), float(signals[NAME_B].mean())
    print(f"  {NAME_A} qty : {qty_a} shares  (~₹{qty_a*avg_pa/1e5:.1f}L notional avg)")
    print(f"  {NAME_B} qty : {qty_b} shares  (~₹{qty_b*avg_pb/1e5:.1f}L notional avg)")
    print(f"  Margin est  : ~₹{(qty_a*avg_pa+qty_b*avg_pb)*0.15/1e5:.1f}L  (15%)\n")

    position = 0
    entry_idx = entry_pa = entry_pb = None
    cooldown  = 0

    trade_records = []
    daily_pnl     = pd.Series(0.0, index=signals.index)

    def close_trade(t, exit_reason):
        nonlocal position, entry_idx, entry_pa, entry_pb, cooldown
        ppa, ppb = pa[t], pb[t]
        if position == 1:
            pnl_a = (ppa - entry_pa) * qty_a
            pnl_b = (entry_pb - ppb) * qty_b
            cost  = ((entry_pa*qty_a + ppa*qty_a) * BROKERAGE_ONEWAY
                     + ppa*qty_a * STT_FUTURES_SELL
                     + (entry_pb*qty_b + ppb*qty_b) * BROKERAGE_ONEWAY
                     + entry_pb*qty_b * STT_FUTURES_SELL)
        else:
            pnl_a = (entry_pa - ppa) * qty_a
            pnl_b = (ppb - entry_pb) * qty_b
            cost  = ((entry_pa*qty_a + ppa*qty_a) * BROKERAGE_ONEWAY
                     + entry_pa*qty_a * STT_FUTURES_SELL
                     + (entry_pb*qty_b + ppb*qty_b) * BROKERAGE_ONEWAY
                     + ppb*qty_b * STT_FUTURES_SELL)
        net = pnl_a + pnl_b - cost
        daily_pnl.iloc[t] += net

        if exit_reason == "stop-loss":
            cooldown = 3

        trade_records.append({
            "entry_date":  dates[entry_idx].date(),
            "exit_date":   dates[t].date(),
            "hold_days":   t - entry_idx,
            "direction":   f"Long{NAME_A}-Short{NAME_B}" if position == 1
                           else f"Short{NAME_A}-Long{NAME_B}",
            "entry_z":     round(zs[entry_idx], 3),
            "exit_z":      round(zs[t], 3),
            "exit_reason": exit_reason,
            "entry_pa":    round(entry_pa, 2),  "exit_pa": round(ppa, 2),
            "entry_pb":    round(entry_pb, 2),  "exit_pb": round(ppb, 2),
            "pnl_a":  round(pnl_a, 2), "pnl_b": round(pnl_b, 2),
            "cost":   round(cost, 2),  "net_pnl": round(net, 2),
            "regime": "active",
        })
        position = 0
        entry_idx = entry_pa = entry_pb = None
        return position  # 0

    for t in range(n):
        z, ppa, ppb = zs[t], pa[t], pb[t]
        in_regime   = bool(regime[t])

        if cooldown > 0:
            cooldown -= 1

        # ── Exit ──────────────────────────────────────────────────────────────
        if position != 0:
            exit_reason = None
            if abs(z) < EXIT_Z:
                exit_reason = "reversion"
            elif abs(z) > STOP_Z:
                exit_reason = "stop-loss"
            elif not in_regime:
                exit_reason = "regime-break"   # NEW: cointegration failed

            if exit_reason:
                position = close_trade(t, exit_reason)

        # ── Entry — only if regime is active ──────────────────────────────────
        if position == 0 and cooldown == 0 and in_regime:
            if   z >  ENTRY_Z:
                position, entry_idx, entry_pa, entry_pb = -1, t, ppa, ppb
            elif z < -ENTRY_Z:
                position, entry_idx, entry_pa, entry_pb =  1, t, ppa, ppb

    # Close open position at last bar
    if position != 0 and entry_idx is not None:
        t = n - 1
        position = close_trade(t, "end-of-data")

    return daily_pnl, pd.DataFrame(trade_records)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — METRICS
# ─────────────────────────────────────────────────────────────────────────────

def compute_metrics(daily_pnl: pd.Series, trade_df: pd.DataFrame) -> tuple:
    equity    = TOTAL_CAPITAL + daily_pnl.cumsum()
    daily_ret = daily_pnl / TOTAL_CAPITAL
    sharpe    = (daily_ret.mean() / daily_ret.std()) * np.sqrt(252) \
                if daily_ret.std() > 0 else 0.0

    rolling_max = equity.cummax()
    drawdown    = (equity - rolling_max) / rolling_max * 100
    max_dd      = drawdown.min()

    max_dd_days = cur = 0
    for v in (drawdown < 0):
        cur = cur + 1 if v else 0
        max_dd_days = max(max_dd_days, cur)

    n  = len(trade_df)
    nw = (trade_df["net_pnl"] > 0).sum() if n else 0
    pf_num = trade_df.loc[trade_df["net_pnl"] > 0, "net_pnl"].sum()
    pf_den = abs(trade_df.loc[trade_df["net_pnl"] <= 0, "net_pnl"].sum())

    return equity, drawdown, dict(
        sharpe=sharpe, max_dd_pct=max_dd, max_dd_days=max_dd_days,
        total_trades=n, win_rate=nw / n * 100 if n else 0,
        net_pnl=daily_pnl.sum(), avg_daily=daily_pnl.mean(),
        avg_hold=trade_df["hold_days"].mean() if n else 0,
        avg_winner=trade_df.loc[trade_df["net_pnl"] > 0, "net_pnl"].mean() if nw else 0,
        avg_loser=trade_df.loc[trade_df["net_pnl"] <= 0, "net_pnl"].mean() if n - nw else 0,
        stops_hit=(trade_df["exit_reason"] == "stop-loss").sum() if n else 0,
        regime_exits=(trade_df["exit_reason"] == "regime-break").sum() if n else 0,
        profit_factor=pf_num / pf_den if pf_den > 0 else np.inf,
        total_days=len(daily_pnl),
    )


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — PLOTS
# ─────────────────────────────────────────────────────────────────────────────

def save_regime_plot(signals: pd.DataFrame):
    """Show rolling ADF and half-life over time with regime shading."""
    fig, axes = plt.subplots(3, 1, figsize=(16, 10), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1.5, 1.5]})

    dates  = signals.index
    regime = signals["regime_active"].values

    # Shade regime periods
    def shade_regime(ax):
        in_regime = False
        start = None
        for i, (d, r) in enumerate(zip(dates, regime)):
            if r and not in_regime:
                start = d; in_regime = True
            elif not r and in_regime:
                ax.axvspan(start, d, alpha=0.12, color="green")
                in_regime = False
        if in_regime:
            ax.axvspan(start, dates[-1], alpha=0.12, color="green")

    # Panel 1: Prices
    ax1 = axes[0]
    ax1r = ax1.twinx()
    ax1.plot(dates, signals[NAME_A], color="#1565C0", lw=1.0, label=f"{NAME_A} (L)")
    ax1r.plot(dates, signals[NAME_B], color="#E65100", lw=1.0, ls="--", label=f"{NAME_B} (R)")
    shade_regime(ax1)
    ax1.set_title(
        f"{NAME_A} / {NAME_B} — V2 Rolling Cointegration Gate\n"
        f"Green shading = cointegration active (ADF < −2.86, HL < {MAX_HL_ROLLING}d)",
        fontsize=11, fontweight="bold")
    ax1.set_ylabel(f"{NAME_A} ₹", fontsize=9, color="#1565C0")
    ax1r.set_ylabel(f"{NAME_B} ₹", fontsize=9, color="#E65100")
    l1, lb1 = ax1.get_legend_handles_labels()
    l2, lb2 = ax1r.get_legend_handles_labels()
    ax1.legend(l1 + l2, lb1 + lb2, loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.2)

    # Panel 2: Rolling ADF
    ax2 = axes[1]
    adf_vals = signals["rolling_adf"].copy()
    adf_vals[adf_vals == 0] = np.nan
    ax2.plot(dates, adf_vals, color="#4A148C", lw=0.9, label="Rolling ADF (spread)")
    ax2.axhline(-2.86, color="#F44336", ls="--", lw=0.9, label="5% critical (−2.86)")
    ax2.axhline(-2.57, color="#FF9800", ls=":",  lw=0.9, label="10% critical (−2.57)")
    shade_regime(ax2)
    ax2.set_ylabel("ADF t-stat", fontsize=9)
    ax2.legend(loc="lower right", fontsize=8)
    ax2.grid(True, alpha=0.2)

    # Panel 3: Rolling half-life
    ax3 = axes[2]
    hl_vals = signals["rolling_hl"].copy()
    hl_vals[hl_vals == np.inf] = np.nan
    hl_vals = hl_vals.clip(upper=150)
    ax3.plot(dates, hl_vals, color="#2E7D32", lw=0.9, label="Rolling half-life (days)")
    ax3.axhline(MAX_HL_ROLLING, color="#F44336", ls="--", lw=0.9,
                label=f"Max allowed ({MAX_HL_ROLLING}d)")
    shade_regime(ax3)
    ax3.set_ylabel("Half-life (days)", fontsize=9)
    ax3.set_xlabel("Date", fontsize=9)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax3.xaxis.set_major_locator(mdates.YearLocator())
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(True, alpha=0.2)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(REGIME_PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Regime plot    → {REGIME_PLOT_PATH}")


def save_equity_plot(daily_pnl, equity, drawdown, m, signals):
    fig, axes = plt.subplots(2, 1, figsize=(16, 8),
                             gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    ax1, ax2 = axes
    dates  = pd.to_datetime(daily_pnl.index)
    regime = signals["regime_active"].reindex(daily_pnl.index).fillna(False).values

    # Shade regime active periods on equity curve
    in_regime = False; rstart = None
    for d, r in zip(dates, regime):
        if r and not in_regime:
            rstart = d; in_regime = True
        elif not r and in_regime:
            ax1.axvspan(rstart, d, alpha=0.07, color="green")
            ax2.axvspan(rstart, d, alpha=0.07, color="green")
            in_regime = False
    if in_regime:
        ax1.axvspan(rstart, dates[-1], alpha=0.07, color="green")
        ax2.axvspan(rstart, dates[-1], alpha=0.07, color="green")

    color = "#2E7D32" if m["net_pnl"] >= 0 else "#C62828"
    ax1.plot(dates, equity, color=color, lw=1.5, label="Portfolio Value")
    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", ls="--", lw=0.8, alpha=0.7,
                label="Starting Capital ₹10L")

    fyers_start = pd.Timestamp("2024-05-28")
    ax1.axvspan(dates.min(), fyers_start, alpha=0.03, color="blue")
    ax1.axvspan(fyers_start, dates.max(), alpha=0.03, color="orange")

    green_patch = mpatches.Patch(color="green", alpha=0.3, label="Cointegration active")
    ax1.set_title(
        f"Pairs Trading V2 — {NAME_A} / {NAME_B} — Rolling Cointegration Gate\n"
        f"Sharpe {m['sharpe']:.2f}  |  Max DD {m['max_dd_pct']:.1f}%  |  "
        f"{m['total_trades']} trades  |  Win {m['win_rate']:.0f}%  |  "
        f"PF {m['profit_factor']:.2f}  |  Net ₹{m['net_pnl']:,.0f}",
        fontsize=11, fontweight="bold")
    ax1.set_ylabel("Portfolio Value (₹)", fontsize=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"₹{x/1e6:.3f}M"))
    h, l = ax1.get_legend_handles_labels()
    ax1.legend(h + [green_patch], l + ["Cointegration active"],
               loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.25)

    ax2.fill_between(dates, drawdown, 0, color="#E53935", alpha=0.55)
    ax2.set_ylabel("Drawdown %", fontsize=9)
    ax2.set_xlabel("Date", fontsize=9)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.grid(True, alpha=0.25)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(EQUITY_CURVE_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Equity curve   → {EQUITY_CURVE_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print(f"  Pairs Trading V2 — {NAME_A} / {NAME_B} — Rolling Cointegration")
    print(f"  {YF_START} → 2026-05-27")
    print(f"  Signal: Lookback {LOOKBACK}d | Entry ±{ENTRY_Z} | Stop ±{STOP_Z}")
    print(f"  Gate  : Coint window {COINT_WINDOW}d | Recheck every {RECHECK_EVERY}d | Max HL {MAX_HL_ROLLING}d | ADF thresh 10%")
    print("=" * 62)

    loader = DataLoader()
    panel  = load_combined_panel(loader)

    # --- Rolling cointegration regime ---
    print("Computing rolling cointegration regime...")
    regime_panel = compute_rolling_regime(panel)

    n_active = regime_panel["regime_active"].sum()
    n_total  = len(regime_panel)
    pct_active = n_active / n_total * 100
    print(f"  Trading active : {n_active} / {n_total} days ({pct_active:.1f}%)")

    # Print regime periods
    print("\n  Regime periods (trading active):")
    regime_arr = regime_panel["regime_active"].values
    dates_arr  = regime_panel.index
    in_r = False; rs = None
    regime_summary = []
    for i, (d, r) in enumerate(zip(dates_arr, regime_arr)):
        if r and not in_r:
            rs = d; in_r = True
        elif not r and in_r:
            adf_at_start = regime_panel.loc[rs:d, "rolling_adf"].iloc[0]
            hl_at_start  = regime_panel.loc[rs:d, "rolling_hl"].iloc[0]
            days = (d - rs).days
            regime_summary.append((rs.date(), d.date(), days, adf_at_start, hl_at_start))
            in_r = False
    if in_r:
        adf_at_start = regime_panel.loc[rs:, "rolling_adf"].iloc[0]
        hl_at_start  = regime_panel.loc[rs:, "rolling_hl"].iloc[0]
        days = (dates_arr[-1] - rs).days
        regime_summary.append((rs.date(), dates_arr[-1].date(), days, adf_at_start, hl_at_start))

    print(f"  {'From':<12} {'To':<12} {'Days':>6}  {'ADF':>8}  {'HL':>8}")
    print(f"  {'-'*55}")
    for rs, re, d, adf, hl in regime_summary:
        hl_str = f"{hl:.0f}d" if np.isfinite(hl) else "∞"
        print(f"  {str(rs):<12} {str(re):<12} {d:>6}  {adf:>8.3f}  {hl_str:>8}")
    print()

    # --- Rolling signals ---
    print("Computing rolling signals...")
    signals = compute_rolling_signals(regime_panel)
    # Attach regime info to signals
    signals["regime_active"] = regime_panel["regime_active"].reindex(signals.index).fillna(False)
    signals["rolling_hl"]    = regime_panel["rolling_hl"].reindex(signals.index)
    signals["rolling_adf"]   = regime_panel["rolling_adf"].reindex(signals.index)

    print(f"Live trading from : {signals.index[0].date()}  "
          f"(after {LOOKBACK}-day warmup)\n")

    # --- Simulate ---
    print("Running simulation...\n")
    daily_pnl, trade_df = simulate(signals)

    if trade_df.empty:
        print("No trades generated.")
        save_regime_plot(signals)
        return

    # --- Metrics ---
    equity, drawdown, m = compute_metrics(daily_pnl, trade_df)

    print(f"\n{'=' * 57}")
    print(f"  RESULTS — {NAME_A}/{NAME_B} — V2 Rolling Coint Gate")
    print(f"{'=' * 57}")
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
    print(f"  Regime exits      : {m['regime_exits']} / {m['total_trades']}")
    print(f"  Net P&L           : ₹{m['net_pnl']:,.0f}")
    print(f"  Avg Daily P&L     : ₹{m['avg_daily']:.2f}")
    print(f"  Total Days Tested : {m['total_days']}")
    print(f"  Days in regime    : {n_active} ({pct_active:.1f}%)")
    print(f"{'=' * 57}")

    # Year-by-year breakdown
    print("\n  Year-by-Year P&L:")
    print(f"  {'Year':<6}  {'Trades':>7}  {'Win%':>6}  {'Net P&L':>12}  {'Regime%':>9}")
    print(f"  {'─'*50}")
    for yr in sorted(trade_df["entry_date"].apply(lambda d: d.year).unique()):
        yr_trades = trade_df[trade_df["entry_date"].apply(lambda d: d.year) == yr]
        yr_regime = signals[signals.index.year == yr]["regime_active"].mean() * 100
        nt = len(yr_trades)
        nw = (yr_trades["net_pnl"] > 0).sum()
        np_ = yr_trades["net_pnl"].sum()
        wr  = nw / nt * 100 if nt else 0
        print(f"  {yr:<6}  {nt:>7}  {wr:>5.0f}%  {np_:>12,.0f}  {yr_regime:>8.0f}%")

    # Save outputs
    trade_df.to_csv(TRADES_PATH, index=False)
    print(f"\n  Trade log CSV  → {TRADES_PATH}")
    save_regime_plot(signals)
    save_equity_plot(daily_pnl, equity, drawdown, m, signals)
    print("\nDone.")


if __name__ == "__main__":
    main()
