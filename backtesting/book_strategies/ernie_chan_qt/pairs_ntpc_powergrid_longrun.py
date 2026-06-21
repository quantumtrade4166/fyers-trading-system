# ============================================================
# pairs_ntpc_powergrid_longrun.py
#
# NTPC / POWERGRID Pairs Trading — 11-Year Backtest
# Same framework as TCS/INFY longrun.
#
# Why this pair:
#   Both are PSU (Govt of India) regulated utilities.
#   Revenue set by the same regulator (CERC).
#   Both are transmission/generation monopolies.
#   No governance divergence risk (govt-controlled).
#   Highly correlated to same macro: interest rates, coal,
#   government capex. Structural breaks almost impossible.
#
# Data sources:
#   2015-01-01 → 2024-05-27 : Yahoo Finance (yfinance, Adj Close)
#   2024-05-28 → 2026-06-15 : Fyers 5-min OHLCV → resampled daily
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
import yfinance as yf

from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

# ─────────────────────────────────────────────────────────────────────────────
# PARAMETERS
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL_A = "NSE:NTPC-EQ"
SYMBOL_B = "NSE:POWERGRID-EQ"
TICKER_A = "NTPC.NS"
TICKER_B = "POWERGRID.NS"
NAME_A   = "NTPC"
NAME_B   = "POWERGRID"

YF_START = "2015-01-01"
YF_END   = "2024-05-27"

LOT_A    = 3250   # NTPC shares per lot (NSE F&O)
LOT_B    = 4200   # POWERGRID: 2 lots (2100x2) — fixes hedge imbalance
#                   OLS beta=1.075 => need 3250x1.075=3495 PG shares = 1.66 lots
#                   Nearest round lot = 2 lots = 4200 shares
#                   Notional match: NTPC 3250xRs115=Rs3.75L | PG 4200xRs97=Rs4.07L ✅
N_LOTS   = 1

# ── Pair-specific parameters (NOT inherited from TCS/INFY) ──────────────────
# Half-life = 124 trading days (measured on Yahoo 2015-2024 data)
# TCS/INFY HL = ~30 days. This pair reverts 4x SLOWER.
#
# Rule: LOOKBACK = 2x half-life for reliable spread estimation
# TCS/INFY used LOOKBACK=63 (2x its ~30d HL) — same principle here
# => LOOKBACK = 2 x 124 = 248 => round to 252 (1 trading year)

LOOKBACK = 252    # 2x half-life (124d) — was 63 (wrong: shorter than HL)
ENTRY_Z  = 2.0    # lower threshold: wider sigma at 252-day window -> fewer signals; 2.0 compensates
EXIT_Z   = 0.5
STOP_Z   = 3.5    # tighter than 4.0 — prevents catastrophic single-trade loss

TOTAL_CAPITAL    = 1_000_000
BROKERAGE_ONEWAY = 0.0003
STT_FUTURES_SELL = 0.0001

# Annual stop calibration for THIS pair:
# At 252-day lookback, spread sigma ~ Rs6/share, stop range = 1.5 sigma = Rs9
# Max loss/trade (NTPC leg) ~ Rs9 x 3250 ~ Rs29K; POWERGRID partially offsets
# Calibrated to 3 losing trades: Rs29K x 3 = Rs87K => use Rs80K
ANNUAL_STOP       = 80_000    # pair-specific: was Rs30K (TCS/INFY calibration — wrong here)
MAX_HALFLIFE_DAYS = 150       # raised from 120: this pair's HL is 124d

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
CACHE_DIR   = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CACHE_FILE        = CACHE_DIR / "ntpc_powergrid_daily_2015_2024.parquet"
EQUITY_CURVE_PATH = RESULTS_DIR / "equity_curve_ntpc_powergrid_v2.png"
TRADES_PATH       = RESULTS_DIR / "trades_ntpc_powergrid_v2.csv"
SPREAD_PLOT_PATH  = RESULTS_DIR / "spread_zscore_ntpc_powergrid_v2.png"


# ─────────────────────────────────────────────────────────────────────────────
# DATA LOAD & STITCH
# ─────────────────────────────────────────────────────────────────────────────

def fetch_yahoo(ticker: str, start: str, end: str) -> pd.Series:
    print(f"  Downloading {ticker} from Yahoo Finance ({start} to {end})...")
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
              f"({panel.index[0].date()} to {panel.index[-1].date()})")
        return panel
    print("  Cache not found — downloading from Yahoo Finance...")
    ntpc  = fetch_yahoo(TICKER_A, YF_START, YF_END)
    pwgr  = fetch_yahoo(TICKER_B, YF_START, YF_END)
    panel = pd.DataFrame({NAME_A: ntpc, NAME_B: pwgr}).dropna()
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
    print("\n-- Loading historical data (Yahoo Finance) --")
    yf_panel = load_yahoo_panel()

    print("\n-- Loading recent data (Fyers 5-min daily) --")
    fy_panel = load_fyers_panel(loader)

    cutoff  = pd.Timestamp(YF_END).normalize()
    yf_trim = yf_panel[yf_panel.index <= cutoff]
    fy_trim = fy_panel[fy_panel.index >  cutoff]

    combined = pd.concat([yf_trim, fy_trim]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")].dropna()

    last_yahoo_date  = yf_trim.index[-1]
    first_fyers_date = fy_trim.index[0] if not fy_trim.empty else None

    print(f"\n-- Stitch: Yahoo ends {last_yahoo_date.date()}, "
          f"Fyers starts {first_fyers_date.date() if first_fyers_date else 'N/A'} --")
    before = combined[combined.index == last_yahoo_date]
    after  = combined[combined.index >  last_yahoo_date].head(1)
    if not before.empty and not after.empty:
        for name in [NAME_A, NAME_B]:
            p_before = before[name].values[0]
            p_after  = after[name].values[0]
            gap_pct  = (p_after - p_before) / p_before * 100
            print(f"  {name}: Yahoo {p_before:.2f}  Fyers {p_after:.2f}  (gap: {gap_pct:+.2f}%)")

    print(f"\n-- Combined panel --")
    print(f"  Total days : {len(combined)}")
    print(f"  Date range : {combined.index[0].date()} to {combined.index[-1].date()}")
    print(f"  Yahoo days : {len(yf_trim)}  ({yf_trim.index[0].date()} to {yf_trim.index[-1].date()})")
    print(f"  Fyers days : {len(fy_trim)}  ({fy_trim.index[0].date()} to {fy_trim.index[-1].date()})")
    print()
    return combined


# ─────────────────────────────────────────────────────────────────────────────
# COINTEGRATION
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


def adf_verdict(t: float, label: str) -> dict:
    cv = {"1%": -3.43, "5%": -2.86, "10%": -2.57}
    if   t < cv["1%"]:  sig, stat, pval = "1%",  "STATIONARY ✅", "< 0.01"
    elif t < cv["5%"]:  sig, stat, pval = "5%",  "STATIONARY ✅", "< 0.05"
    elif t < cv["10%"]: sig, stat, pval = "10%", "STATIONARY (weak)", "< 0.10"
    else:               sig, stat, pval = "--",  "NON-STATIONARY ❌", "> 0.10"
    return {"label": label, "t_stat": t, "sig": sig,
            "status": stat, "pval": pval, **cv}


def compute_halflife(spread: pd.Series) -> float:
    s = spread.dropna().values
    dy, lag = np.diff(s), s[:-1]
    X = np.column_stack([np.ones(len(lag)), lag])
    try:
        coeffs, _, _, _ = np.linalg.lstsq(X, dy, rcond=None)
        phi = float(coeffs[1])
        return float(-np.log(2) / np.log(1 + phi)) if phi < 0 else np.inf
    except Exception:
        return np.inf


def run_cointegration_suite(panel: pd.DataFrame) -> tuple:
    price_a = panel[NAME_A].values
    price_b = panel[NAME_B].values
    X       = np.column_stack([np.ones(len(price_b)), price_b])
    coeffs, _, _, _ = np.linalg.lstsq(X, price_a, rcond=None)
    alpha, beta     = float(coeffs[0]), float(coeffs[1])
    spread          = pd.Series(price_a - beta * price_b, index=panel.index)

    r_a   = adf_verdict(adf_stat(panel[NAME_A]), f"{NAME_A} price")
    r_b   = adf_verdict(adf_stat(panel[NAME_B]), f"{NAME_B} price")
    r_spr = adf_verdict(adf_stat(spread),         "Spread (residual)")
    hl    = compute_halflife(spread)

    sep = "=" * 62
    print(sep)
    print(f"  COINTEGRATION REPORT — {NAME_A} / {NAME_B}  (11-year sample)")
    print(sep)
    print(f"\n  OLS: {NAME_A} = {alpha:.2f} + {beta:.4f} x {NAME_B}")
    print(f"  Hedge ratio beta : {beta:.4f}")
    print(f"  {'Series':<24} {'ADF t-stat':>12} {'p-value':>10}   Result")
    print(f"  {'-'*60}")
    for r in [r_a, r_b, r_spr]:
        print(f"  {r['label']:<24} {r['t_stat']:>12.3f} {r['pval']:>10}   {r['status']}")
    print(f"\n  Critical values: 1% {r_spr['1%']}  5% {r_spr['5%']}  10% {r_spr['10%']}")
    hl_str = f"{hl:.1f} trading days" if hl != np.inf else "inf (not mean-reverting)"
    print(f"  Half-life of mean reversion : {hl_str}")

    spread_ok = r_spr["t_stat"] < r_spr["10%"]
    hl_ok     = hl != np.inf and 1 < hl < MAX_HALFLIFE_DAYS

    tick = lambda ok: "YES" if ok else "NO"
    print(f"\n  CHECKLIST:")
    print(f"    [{tick(r_a['t_stat'] > r_a['10%'])}] {NAME_A} price non-stationary  (t={r_a['t_stat']:.3f})")
    print(f"    [{tick(r_b['t_stat'] > r_b['10%'])}] {NAME_B} price non-stationary  (t={r_b['t_stat']:.3f})")
    print(f"    [{tick(spread_ok)}] Spread stationary          (t={r_spr['t_stat']:.3f}, need < {r_spr['10%']})")
    print(f"    [{tick(hl_ok)}] Half-life 1-{MAX_HALFLIFE_DAYS} days       ({hl_str})")

    passed = spread_ok and hl_ok
    print(f"\n  VERDICT: {'PAIR QUALIFIES ✅' if passed else 'PAIR DOES NOT QUALIFY ❌'}")
    print(sep + "\n")
    return beta, spread, passed, hl


# ─────────────────────────────────────────────────────────────────────────────
# ROLLING SIGNALS
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
# SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def calc_trade_pnl(position, entry_pa, entry_pb, ppa, ppb, qty_a, qty_b):
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
    return pnl_a, pnl_b, cost, pnl_a + pnl_b - cost


def simulate(signals: pd.DataFrame) -> tuple:
    dates   = signals.index.tolist()
    pa      = signals[NAME_A].values
    pb      = signals[NAME_B].values
    zs      = signals["zscore"].values
    n       = len(signals)
    qty_a, qty_b = N_LOTS * LOT_A, N_LOTS * LOT_B

    avg_pa = float(signals[NAME_A].mean())
    avg_pb = float(signals[NAME_B].mean())
    print(f"  {NAME_A} qty : {qty_a} shares  (~Rs{qty_a*avg_pa/1e5:.1f}L notional avg)")
    print(f"  {NAME_B} qty : {qty_b} shares  (~Rs{qty_b*avg_pb/1e5:.1f}L notional avg)")
    print(f"  Margin est  : ~Rs{(qty_a*avg_pa+qty_b*avg_pb)*0.15/1e5:.1f}L  (15%)")
    if ANNUAL_STOP:
        print(f"  Annual P&L stop : Rs{ANNUAL_STOP:,.0f} loss cap per calendar year")
    print()

    position  = 0
    entry_idx = entry_pa = entry_pb = None
    cooldown  = 0
    annual_pnl    = 0.0
    annual_halted = False
    current_year  = dates[0].year

    trade_records = []
    daily_pnl     = pd.Series(0.0, index=signals.index)

    for t in range(n):
        z, ppa, ppb = zs[t], pa[t], pb[t]
        bar_year = dates[t].year

        if bar_year != current_year:
            if annual_halted:
                print(f"    Annual stop lifted. Resuming {bar_year}.")
            current_year  = bar_year
            annual_pnl    = 0.0
            annual_halted = False

        if cooldown > 0:
            cooldown -= 1

        if position != 0:
            exit_reason = None
            if abs(z) < EXIT_Z:
                exit_reason = "reversion"
            elif abs(z) > STOP_Z:
                exit_reason = "stop-loss"
                cooldown = 5   # longer recovery for slow-reverting pair
            elif ANNUAL_STOP and annual_halted:
                exit_reason = "annual-stop"

            if exit_reason:
                pnl_a, pnl_b, cost, net = calc_trade_pnl(
                    position, entry_pa, entry_pb, ppa, ppb, qty_a, qty_b)
                daily_pnl.iloc[t] += net
                annual_pnl        += net

                trade_records.append({
                    "entry_date":  dates[entry_idx].date(),
                    "exit_date":   dates[t].date(),
                    "hold_days":   t - entry_idx,
                    "direction":   f"Long{NAME_A}-Short{NAME_B}" if position == 1
                                   else f"Short{NAME_A}-Long{NAME_B}",
                    "entry_z":     round(zs[entry_idx], 3),
                    "exit_z":      round(z, 3),
                    "exit_reason": exit_reason,
                    "entry_pa":    round(entry_pa, 2), "exit_pa": round(ppa, 2),
                    "entry_pb":    round(entry_pb, 2), "exit_pb": round(ppb, 2),
                    "pnl_a": round(pnl_a, 2), "pnl_b": round(pnl_b, 2),
                    "cost": round(cost, 2), "net_pnl": round(net, 2),
                })
                position = 0
                entry_idx = entry_pa = entry_pb = None

                if ANNUAL_STOP and annual_pnl <= -ANNUAL_STOP and not annual_halted:
                    annual_halted = True
                    print(f"    ANNUAL STOP hit {bar_year} "
                          f"(year P&L: Rs{annual_pnl:,.0f}). "
                          f"No new entries until {bar_year + 1}.")

        if position == 0 and cooldown == 0 and not annual_halted:
            if   z >  ENTRY_Z:
                position, entry_idx, entry_pa, entry_pb = -1, t, ppa, ppb
            elif z < -ENTRY_Z:
                position, entry_idx, entry_pa, entry_pb =  1, t, ppa, ppb

    # Close open position at end
    if position != 0 and entry_idx is not None:
        t = n - 1
        ppa, ppb = pa[t], pb[t]
        pnl_a, pnl_b, cost, net = calc_trade_pnl(
            position, entry_pa, entry_pb, ppa, ppb, qty_a, qty_b)
        daily_pnl.iloc[t] += net
        trade_records.append({
            "entry_date": dates[entry_idx].date(), "exit_date": dates[t].date(),
            "hold_days": t - entry_idx,
            "direction": f"Long{NAME_A}-Short{NAME_B}" if position == 1
                         else f"Short{NAME_A}-Long{NAME_B}",
            "entry_z": round(zs[entry_idx], 3), "exit_z": round(zs[t], 3),
            "exit_reason": "end-of-data",
            "entry_pa": round(entry_pa, 2), "exit_pa": round(ppa, 2),
            "entry_pb": round(entry_pb, 2), "exit_pb": round(ppb, 2),
            "pnl_a": round(pnl_a, 2), "pnl_b": round(pnl_b, 2),
            "cost": round(cost, 2), "net_pnl": round(net, 2),
        })

    return daily_pnl, pd.DataFrame(trade_records)


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
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

    in_position = (daily_pnl != 0).sum()

    return equity, drawdown, dict(
        sharpe=sharpe, max_dd_pct=max_dd, max_dd_days=max_dd_days,
        total_trades=n, win_rate=nw / n * 100 if n else 0,
        net_pnl=daily_pnl.sum(), avg_daily=daily_pnl.mean(),
        avg_hold=trade_df["hold_days"].mean() if n else 0,
        avg_winner=trade_df.loc[trade_df["net_pnl"] > 0, "net_pnl"].mean() if nw else 0,
        avg_loser=trade_df.loc[trade_df["net_pnl"] <= 0, "net_pnl"].mean() if n - nw else 0,
        stops_hit=(trade_df["exit_reason"] == "stop-loss").sum() if n else 0,
        profit_factor=pf_num / pf_den if pf_den > 0 else float("inf"),
        total_days=len(daily_pnl),
        days_deployed=int(in_position),
        pct_deployed=in_position / len(daily_pnl) * 100,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PLOTS
# ─────────────────────────────────────────────────────────────────────────────

def save_spread_plot(signals: pd.DataFrame):
    fig, axes = plt.subplots(3, 1, figsize=(16, 11), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1.5, 1.5]})
    dates = signals.index

    ax1, ax1r = axes[0], axes[0].twinx()
    ax1.plot(dates,  signals[NAME_A], color="#1565C0", lw=1.0, label=f"{NAME_A} (L)")
    ax1r.plot(dates, signals[NAME_B], color="#E65100", lw=1.0, ls="--", label=f"{NAME_B} (R)")
    ax1.set_title(
        f"{NAME_A} vs {NAME_B} — 11-Year Pairs Trading\n"
        f"2015-2026 | Lookback={LOOKBACK}d | Entry +/-{ENTRY_Z} | Exit +/-{EXIT_Z} | Stop +/-{STOP_Z}",
        fontsize=11, fontweight="bold")
    ax1.set_ylabel(f"{NAME_A} Rs", fontsize=9, color="#1565C0")
    ax1r.set_ylabel(f"{NAME_B} Rs", fontsize=9, color="#E65100")
    l1, lb1 = ax1.get_legend_handles_labels()
    l2, lb2 = ax1r.get_legend_handles_labels()
    ax1.legend(l1 + l2, lb1 + lb2, loc="upper left", fontsize=8)
    ax1.grid(True, alpha=0.2)

    ax2 = axes[1]
    ax2.plot(dates, signals["spread"], color="#4A148C", lw=0.8, label="Rolling Spread")
    ax2.axhline(0, color="#9E9E9E", ls="--", lw=0.7)
    ax2.set_ylabel("Spread Rs", fontsize=9)
    ax2.legend(loc="upper left", fontsize=8)
    ax2.grid(True, alpha=0.2)

    ax3 = axes[2]
    ax3.plot(dates, signals["zscore"], color="#2E7D32", lw=0.8, label="Z-score")
    for level, color, style, lbl in [
        (ENTRY_Z,  "#F44336", "--", f"Entry +/-{ENTRY_Z}"), (-ENTRY_Z, "#F44336", "--", None),
        (EXIT_Z,   "#66BB6A", ":",  f"Exit +/-{EXIT_Z}"),   (-EXIT_Z,  "#66BB6A", ":",  None),
        (STOP_Z,   "#000000", ":",  f"Stop +/-{STOP_Z}"),   (-STOP_Z,  "#000000", ":",  None),
    ]:
        ax3.axhline(level, color=color, ls=style, lw=0.9,
                    label=lbl if lbl else "_nolegend_")
    ax3.set_ylabel("Z-score", fontsize=9)
    ax3.set_xlabel("Date", fontsize=9)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax3.xaxis.set_major_locator(mdates.YearLocator())
    ax3.legend(loc="upper left", fontsize=8)
    ax3.grid(True, alpha=0.2)

    plt.xticks(rotation=30, ha="right")
    plt.tight_layout()
    plt.savefig(SPREAD_PLOT_PATH, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Spread plot saved")


def save_equity_plot(daily_pnl, equity, drawdown, m):
    fig, axes = plt.subplots(2, 1, figsize=(16, 8),
                             gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    ax1, ax2 = axes
    dates = pd.to_datetime(daily_pnl.index)

    color = "#2E7D32" if m["net_pnl"] >= 0 else "#C62828"
    ax1.plot(dates, equity, color=color, lw=1.5)
    ax1.axhline(TOTAL_CAPITAL, color="#9E9E9E", ls="--", lw=0.8, alpha=0.7,
                label="Starting Capital Rs10L")

    fyers_start = pd.Timestamp("2024-05-28")
    ax1.axvspan(dates.min(), fyers_start, alpha=0.04, color="blue", label="Yahoo Finance data")
    ax1.axvspan(fyers_start, dates.max(), alpha=0.04, color="green", label="Fyers data")

    ax1.set_title(
        f"Pairs Trading — {NAME_A} / {NAME_B} — 11 Years (2015-2026)\n"
        f"Sharpe {m['sharpe']:.2f}  |  Max DD {m['max_dd_pct']:.1f}%  |  "
        f"{m['total_trades']} trades  |  Win {m['win_rate']:.0f}%  |  "
        f"PF {m['profit_factor']:.2f}  |  Net Rs{m['net_pnl']:,.0f}",
        fontsize=11, fontweight="bold")
    ax1.set_ylabel("Portfolio Value (Rs)", fontsize=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"Rs{x/1e6:.3f}M"))
    ax1.legend(loc="upper left", fontsize=8)
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
    print(f"  Equity curve saved")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print(f"  Pairs Trading — {NAME_A} / {NAME_B} — 11-Year Backtest")
    print(f"  {YF_START} to 2026")
    print(f"  Lots: {N_LOTS} | Lookback: {LOOKBACK}d | Entry +/-{ENTRY_Z} | Stop +/-{STOP_Z}")
    print(f"  Annual stop: Rs{ANNUAL_STOP:,.0f}")
    print("=" * 62)

    loader = DataLoader()
    panel  = load_combined_panel(loader)

    beta, _, passed, hl = run_cointegration_suite(panel)
    if not passed:
        print("STOP: Cointegration failed. Saving spread plot and exiting.")
        signals = compute_rolling_signals(panel)
        if not signals.empty:
            save_spread_plot(signals)
        return

    print(f"Pair qualifies. Half-life = {hl:.1f} days.\n")

    print("Computing rolling signals...")
    signals = compute_rolling_signals(panel)
    print(f"Live trading from : {signals.index[0].date()}  "
          f"(after {LOOKBACK}-day warmup)\n")

    print("Running simulation...\n")
    daily_pnl, trade_df = simulate(signals)
    if trade_df.empty:
        print("No trades generated.")
        return

    equity, drawdown, m = compute_metrics(daily_pnl, trade_df)

    print(f"\n{'=' * 57}")
    print(f"  RESULTS — {NAME_A} / {NAME_B} — 11 YEARS")
    print(f"{'=' * 57}")
    print(f"  Sharpe Ratio      : {m['sharpe']:.3f}")
    print(f"  Max Drawdown %    : {m['max_dd_pct']:.2f}%")
    print(f"  Max DD Duration   : {m['max_dd_days']} days")
    print(f"  Total Trades      : {m['total_trades']}")
    print(f"  Win Rate %        : {m['win_rate']:.1f}%")
    print(f"  Profit Factor     : {m['profit_factor']:.2f}")
    print(f"  Avg Hold (days)   : {m['avg_hold']:.1f}")
    print(f"  Avg Winner        : Rs{m['avg_winner']:,.0f}")
    print(f"  Avg Loser         : Rs{m['avg_loser']:,.0f}")
    annual_stop_exits = (trade_df["exit_reason"] == "annual-stop").sum()
    print(f"  Stop-losses hit   : {m['stops_hit']} / {m['total_trades']}")
    print(f"  Annual stop exits : {annual_stop_exits} / {m['total_trades']}")
    print(f"  Days deployed     : {m['days_deployed']} / {m['total_days']} ({m['pct_deployed']:.1f}%)")
    print(f"  Net P&L           : Rs{m['net_pnl']:,.0f}")
    print(f"  Avg Daily P&L     : Rs{m['avg_daily']:.2f}")
    print(f"{'=' * 57}")

    trade_df["year"] = pd.to_datetime(trade_df["exit_date"]).dt.year
    print(f"\n  Year-by-Year P&L:")
    print(f"  {'Year':<6} {'Trades':>7} {'Win%':>7} {'Net P&L':>12}  Note")
    print(f"  {'─'*55}")
    for yr, grp in trade_df.groupby("year"):
        wr   = (grp["net_pnl"] > 0).mean() * 100
        note = "ANNUAL STOP" if (grp["exit_reason"] == "annual-stop").any() else ""
        print(f"  {yr:<6} {len(grp):>7} {wr:>6.0f}% {grp['net_pnl'].sum():>12,.0f}  {note}")
    print()

    trade_df.to_csv(TRADES_PATH, index=False)
    print(f"  Trade log  : {TRADES_PATH.name}")
    save_spread_plot(signals)
    save_equity_plot(daily_pnl, equity, drawdown, m)
    print("\nDone.")


if __name__ == "__main__":
    main()
