"""
mtm_equity.py — True mark-to-market equity curve for StatArb.MR-first10

portfolio_backtest.py books all P&L on the exit date only.
Positions in flight show zero daily P&L — the equity curve looks flat
while positions are open, understating volatility and inflating Sharpe.

This script marks unrealised P&L at every closing price:
  - Entry brokerage charged on entry day
  - Exit brokerage charged on exit day
  - Equity moves every day a position is open

Output:
  results/mtm_daily.csv          — daily equity, daily pnl, drawdown
  results/mtm_equity_curve.png   — MTM curve vs exit-day curve (overlay)

Trade logic is identical to portfolio_backtest.py (same signals, same exits).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
import numpy as np
import pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STOCK_DIR    = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/data/stocks"
DATA_DIR     = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/data"
RESULTS_DIR  = PROJECT_ROOT / "backtesting/book_strategies/ernie_chan_qt/results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

EXIT_Z = 0.5
COOLDOWN = 5
BROK = 0.0003
SPAN_R = 0.15

# (label, symA, yf_A, qty_A, symB, yf_B, qty_B, lookback, entry_z, stop_z, annual_stop)
PAIRS = [
    ("TCS/INFY",              "TCS",        "TCS.NS",        450,   "INFY",       "INFY.NS",        900,  126, 2.0, 3.5,    58_000),
    ("BAJAJFINSV/BAJFINANCE", "BAJAJFINSV", "BAJAJFINSV.NS", 2000,  "BAJFINANCE", "BAJFINANCE.NS",  4125, 166, 2.5, 3.0,   209_295),
    ("HDFCBANK/KOTAKBANK",    "HDFCBANK",   "HDFCBANK.NS",   5500,  "KOTAKBANK",  "KOTAKBANK.NS",  12400, 113, 2.0, 4.0,   393_340),
    ("HINDUNILVR/DABUR",      "HINDUNILVR", "HINDUNILVR.NS", 3000,  "DABUR",      "DABUR.NS",      16250,  67, 1.5, 4.0,   508_632),
    ("OBEROIRLTY/BRIGADE",    "OBEROIRLTY", "OBEROIRLTY.NS", 7200,  "BRIGADE",    "BRIGADE.NS",    14000,  92, 1.5, 3.5, 1_137_471),
    ("TATAPOWER/JSWENERGY",   "TATAPOWER",  "TATAPOWER.NS",  39600, "JSWENERGY",  "JSWENERGY.NS",  26000,  76, 2.0, 3.0, 1_211_814),
    ("TECHM/COFORGE",         "TECHM",      "TECHM.NS",      4800,  "COFORGE",    "COFORGE.NS",     3800, 140, 1.5, 3.5,   501_534),
    ("EICHERMOT/TVSMOTORS",   "EICHERMOT",  "EICHERMOT.NS",  1400,  "TVSMOTORS",  "TVSMOTOR.NS",    2100, 129, 1.5, 4.0,   554_023),
    ("HDFCLIFE/ICICIPRULI",   "HDFCLIFE",   "HDFCLIFE.NS",   3300,  "ICICIPRULI", "ICICIPRULI.NS",  3000,  87, 2.0, 4.0,   223_222),
    ("SRF/DEEPAKNTR",         "SRF",        "SRF.NS",        2500,  "DEEPAKNTR",  "DEEPAKNTR.NS",   2450, 151, 2.0, 4.0, 1_052_772),
]

LEGACY_FILES = {
    "TCS/INFY":              (DATA_DIR / "tcs_infy_daily_2015_2024_ext.parquet",    "TCS",        "INFY"),
    "BAJAJFINSV/BAJFINANCE": (DATA_DIR / "BAJFINANCE_BAJAJFINSV_daily.parquet",     "BAJAJFINSV", "BAJFINANCE"),
}


# ── Data loading (same as portfolio_backtest.py) ──────────────────────────────

def load_stock(sym, yf_ticker):
    for suffix in ("_ext", "_yf"):
        p = STOCK_DIR / f"{sym}{suffix}.parquet"
        if p.exists():
            raw = pd.read_parquet(p)
            s = raw.iloc[:, 0] if isinstance(raw, pd.DataFrame) else raw
            s.index = pd.to_datetime(s.index)
            return s.sort_index().dropna()
    import yfinance as yf
    raw = yf.download(yf_ticker, start="2015-01-01", end="2024-05-28",
                      progress=False, auto_adjust=True)
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


# ── Signal computation (identical to portfolio_backtest.py) ───────────────────

def compute_signals(pa, pb, lookback):
    n = len(pa)
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


# ── MTM simulator ─────────────────────────────────────────────────────────────

def simulate_mtm(pa, pb, dates, zscores, half_lives,
                 n_A_sh, n_B_sh, entry_z, stop_z, annual_stop, max_hl, initial_cap):
    """
    Identical trade logic to portfolio_backtest.simulate().
    Key difference: equity is marked at every bar using unrealised P&L.

    Brokerage accounting:
      - Entry brokerage (0.03% of entry notional) charged on entry day
      - Exit brokerage  (0.03% of exit notional)  charged on exit day
      - While open: equity = initial_cap + realized + gross_unrealized - entry_brok
    """
    pos = 0; epa = epb = 0.0
    yrpnl = 0.0; curyr = dates[0].year; cdend = 0
    realized   = 0.0
    entry_brok = 0.0

    n = len(pa)
    equity = np.full(n, float(initial_cap))

    for t in range(n):
        # If z is NaN (warm-up bars), mark at current unrealised (if in trade) or flat
        if np.isnan(zscores[t]):
            if pos != 0:
                gross = ((pa[t] - epa) * n_A_sh - (pb[t] - epb) * n_B_sh) * pos
                equity[t] = initial_cap + realized + gross - entry_brok
            else:
                equity[t] = initial_cap + realized
            continue

        if dates[t].year != curyr:
            curyr = dates[t].year
            yrpnl = 0.0

        z, hl = zscores[t], half_lives[t]

        # ── Position open: check exits, then mark ────────────────────────────
        if pos != 0:
            gross = ((pa[t] - epa) * n_A_sh - (pb[t] - epb) * n_B_sh) * pos

            er = None
            if pos ==  1 and z >= -EXIT_Z:         er = "z_exit"
            if pos == -1 and z <=  EXIT_Z:         er = "z_exit"
            if abs(z) >= stop_z:                   er = "z_stop"
            if (yrpnl + gross) < -annual_stop:     er = "annual_stop"

            if er:
                exit_brok  = (pa[t]*n_A_sh + pb[t]*n_B_sh) * BROK
                net        = gross - entry_brok - exit_brok
                yrpnl     += net
                realized  += net
                pos        = 0
                cdend      = t + COOLDOWN
                entry_brok = 0.0
                equity[t]  = initial_cap + realized
            else:
                equity[t] = initial_cap + realized + gross - entry_brok
            continue

        # ── Flat: mark at realized, check entry ──────────────────────────────
        equity[t] = initial_cap + realized

        if t < cdend or yrpnl < -annual_stop or hl > max_hl:
            continue

        if z < -entry_z:
            pos = 1; epa = pa[t]; epb = pb[t]
            entry_brok = (epa*n_A_sh + epb*n_B_sh) * BROK
        elif z > entry_z:
            pos = -1; epa = pa[t]; epb = pb[t]
            entry_brok = (epa*n_A_sh + epb*n_B_sh) * BROK

    # Force-close any open position at end of data
    if pos != 0:
        t = n - 1
        gross      = ((pa[t] - epa) * n_A_sh - (pb[t] - epb) * n_B_sh) * pos
        exit_brok  = (pa[t]*n_A_sh + pb[t]*n_B_sh) * BROK
        net        = gross - entry_brok - exit_brok
        realized  += net
        equity[t]  = initial_cap + realized

    return pd.Series(equity, index=dates)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    SEP  = "=" * 72
    SEP2 = "─" * 72

    print(SEP)
    print("  MTM EQUITY — StatArb.MR-first10")
    print("  Unrealised P&L marked at every close. Entry/exit brok charged on day.")
    print(SEP)

    pair_results = []

    for row in PAIRS:
        label, symA, yf_A, n_A_sh, symB, yf_B, n_B_sh, \
            lookback, entry_z, stop_z, annual_stop = row

        print(f"\n  {label}")
        try:
            sA, sB = load_pair(label, symA, yf_A, symB, yf_B)
            df = pd.DataFrame({symA: sA, symB: sB}).dropna()
            if len(df) < 500:
                print(f"  SKIP — only {len(df)} rows"); continue

            pa    = df[symA].values
            pb    = df[symB].values
            dates = df.index

            # Full-period OLS → max_hl gate
            _, beta_full = OLS(pa, add_constant(pb)).fit().params
            sp_full      = pa - beta_full * pb
            phi_full     = OLS(np.diff(sp_full), add_constant(sp_full[:-1])).fit().params[1]
            hl_full      = -np.log(2) / np.log(1 + phi_full) if phi_full < 0 else 999
            max_hl       = hl_full * 2.0

            cap = int((pa[-252:].mean() * n_A_sh + pb[-252:].mean() * n_B_sh) * SPAN_R)
            print(f"  SPAN Rs{cap/1e5:.2f}L  HL={hl_full:.1f}d  max_hl={max_hl:.1f}d")
            print(f"  Computing signals (LB={lookback})...")

            zs, hls = compute_signals(pa, pb, lookback)
            eq = simulate_mtm(pa, pb, dates, zs, hls,
                              n_A_sh, n_B_sh, entry_z, stop_z,
                              annual_stop, max_hl, cap)

            final_pnl = eq.iloc[-1] - cap
            print(f"  MTM P&L: Rs{final_pnl/1e5:+.2f}L")
            pair_results.append((label, eq, cap))

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback; traceback.print_exc()

    if not pair_results:
        print("No pairs processed."); sys.exit(1)

    # ── Portfolio daily MTM ───────────────────────────────────────────────────
    total_cap = sum(c for _, _, c in pair_results)

    all_dates = set()
    for _, eq, _ in pair_results:
        all_dates.update(eq.index)
    port_idx = pd.DatetimeIndex(sorted(all_dates))

    port_equity = pd.Series(float(total_cap), index=port_idx)
    for label, eq, cap in pair_results:
        # Fill missing dates: before pair started → cap (zero P&L contribution)
        eq_aligned = eq.reindex(port_idx).ffill().fillna(cap)
        port_equity += eq_aligned - cap

    # ── Stats ─────────────────────────────────────────────────────────────────
    daily_pnl = port_equity.diff().fillna(0.0)
    port_ret  = daily_pnl / total_cap
    dd_series = (port_equity - port_equity.cummax()) / port_equity.cummax() * 100

    sharpe_mtm = port_ret.mean() / port_ret.std() * np.sqrt(252) if port_ret.std() > 0 else 0.0
    nyr        = (port_idx[-1] - port_idx[0]).days / 365.25
    cagr_mtm   = ((port_equity.iloc[-1] / total_cap) ** (1/nyr) - 1) * 100
    maxdd_mtm  = dd_series.min()
    total_pnl  = port_equity.iloc[-1] - total_cap

    print(f"\n{SEP}")
    print(f"  PORTFOLIO MTM RESULTS")
    print(f"  {SEP2}")
    print(f"  Initial SPAN capital      : Rs{total_cap/1e5:.2f}L")
    print(f"  Final MTM equity          : Rs{port_equity.iloc[-1]/1e5:.2f}L")
    print(f"  Total net P&L             : Rs{total_pnl/1e5:+.2f}L")
    print(f"  Sharpe (MTM daily)        : {sharpe_mtm:.3f}")
    print(f"  CAGR on SPAN (MTM)        : {cagr_mtm:.1f}%")
    print(f"  Max Drawdown (MTM)        : {maxdd_mtm:.2f}%")
    print(f"  Period                    : {port_idx[0].date()} → {port_idx[-1].date()}")
    print(f"  {SEP2}")
    print(f"  Sharpe comparison:")
    print(f"    Exit-day only  (portfolio_backtest.py) : 1.636  ← inflated, don't use")
    print(f"    NAV-based      (jensens_alpha.py)      : 0.995")
    print(f"    MTM daily      (this script)           : {sharpe_mtm:.3f}  ← most accurate")

    # ── Year-by-year MTM breakdown ────────────────────────────────────────────
    print(f"\n  YEAR-BY-YEAR MTM")
    print(f"  {'Year':<6} {'P&L':>12} {'Max DD%':>9}")
    print(f"  {SEP2}")
    for yr in sorted(set(port_idx.year)):
        mask   = port_idx.year == yr
        ypnl   = daily_pnl[mask].sum()
        yr_eq  = port_equity[mask]
        yr_dd  = ((yr_eq - yr_eq.cummax()) / yr_eq.cummax() * 100).min() if len(yr_eq) > 1 else 0.0
        sign   = "+" if ypnl >= 0 else "-"
        print(f"  {yr:<6} {sign}Rs{abs(ypnl)/1e5:>7.2f}L  {yr_dd:>8.2f}%")
    print(f"  {SEP2}")
    print(SEP)

    # ── Save CSV ──────────────────────────────────────────────────────────────
    mtm_df = pd.DataFrame({
        "date":         port_idx.strftime("%Y-%m-%d"),
        "equity":       port_equity.round(0).astype(int).values,
        "daily_pnl":    daily_pnl.round(0).astype(int).values,
        "drawdown_pct": dd_series.round(4).values,
    })
    csv_path = RESULTS_DIR / "mtm_daily.csv"
    mtm_df.to_csv(csv_path, index=False)
    print(f"\n  Saved CSV : {csv_path}")

    # ── Plot ──────────────────────────────────────────────────────────────────
    old_csv = RESULTS_DIR / "portfolio_daily_equity.csv"
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8),
                                    gridspec_kw={"height_ratios": [3, 1]},
                                    sharex=True)
    fig.patch.set_facecolor("#ffffff")

    dates_plot = pd.to_datetime(port_idx)
    eq_L  = port_equity / 1e5
    base  = total_cap / 1e5

    # MTM equity curve
    ax1.fill_between(dates_plot, base, eq_L, alpha=0.15, color="#1D9E75")
    ax1.plot(dates_plot, eq_L, color="#1D9E75", linewidth=1.6,
             label=f"MTM equity  Sharpe={sharpe_mtm:.3f}  CAGR={cagr_mtm:.1f}%")

    # Overlay exit-day-only curve for comparison
    if old_csv.exists():
        old = pd.read_csv(old_csv, parse_dates=["date"]).set_index("date")
        old_eq = (old["equity"] / 1e5).reindex(dates_plot, method="ffill")
        ax1.plot(dates_plot, old_eq.values, color="#AAAAAA", linewidth=1.0,
                 linestyle="--", alpha=0.7, label="Exit-day only  Sharpe=1.636  (portfolio_backtest.py)")

    ax1.axhline(base, color="#888", linewidth=0.8, linestyle=":",
                alpha=0.5, label=f"Initial SPAN Rs{base:.0f}L")
    ax1.set_ylabel("Portfolio equity (Rs L)", fontsize=11)
    ax1.set_title(
        f"StatArb.MR-first10 — MTM Equity Curve  |  "
        f"Sharpe {sharpe_mtm:.3f}  CAGR {cagr_mtm:.1f}%  Max DD {maxdd_mtm:.1f}%",
        fontsize=12, pad=10)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"Rs{x:.0f}L"))
    ax1.legend(fontsize=9, framealpha=0.4, loc="upper left")
    ax1.grid(True, alpha=0.2, linewidth=0.5)
    ax1.set_facecolor("#fafafa")

    # MTM drawdown
    ax2.fill_between(dates_plot, dd_series.values, 0, alpha=0.4, color="#D85A30")
    ax2.plot(dates_plot, dd_series.values, color="#D85A30", linewidth=1.0,
             label=f"MTM drawdown  max={maxdd_mtm:.1f}%")

    if old_csv.exists():
        old_dd = old["drawdown_pct"].reindex(dates_plot, method="ffill")
        ax2.plot(dates_plot, old_dd.values, color="#AAAAAA", linewidth=0.8,
                 linestyle="--", alpha=0.6, label="Exit-day DD")

    ax2.set_ylabel("Drawdown %", fontsize=11)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.0f}%"))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax2.xaxis.set_major_locator(mdates.YearLocator())
    ax2.legend(fontsize=9, framealpha=0.4)
    ax2.grid(True, alpha=0.2, linewidth=0.5)
    ax2.set_facecolor("#fafafa")

    plt.tight_layout()
    plot_path = RESULTS_DIR / "mtm_equity_curve.png"
    fig.savefig(plot_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"  Saved plot: {plot_path}")
    print(SEP)
