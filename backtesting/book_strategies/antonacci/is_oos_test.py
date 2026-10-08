"""
DualMom.Liq.Nifty50 — In-Sample vs Out-of-Sample validation
IS  : 2006-01-01 to 2015-12-31
OOS : 2016-01-01 to 2026-06-18
Full: 2006-01-01 to 2026-06-18

Same parameters throughout — no re-tuning between periods.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
import yfinance as yf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

DATA_DIR       = Path(r"G:\fyers_data_pipeline\Nifty 500 Daily Data")
LOOKBACK_DAYS  = 252
TOP_N          = 50
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"
IS_END         = "2015-12-31"
OOS_START      = "2016-01-01"

print("Downloading Nifty 50...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty     = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

# Nifty annual returns for benchmark
nifty_ye  = nifty.resample("YE").last()
nifty_ys  = nifty.resample("YS").first()
nifty_ann = {}
for yr_ts in nifty_ye.index:
    yr = yr_ts.year
    sp = nifty_ys[nifty_ys.index.year == yr]
    ep = nifty_ye[nifty_ye.index.year == yr]
    if len(sp) and len(ep) and sp.iloc[0] > 0:
        nifty_ann[yr] = (ep.iloc[0] / sp.iloc[0] - 1) * 100

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]
monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Matrix: {prices.shape}\n")


def run_backtest(label, date_filter_start=None, date_filter_end=None):
    """
    Always runs on full price history (for correct MA calculation),
    but only records NAV for months within [date_filter_start, date_filter_end].
    Capital resets to 1M at the start of the recording window.
    """
    cash_value  = CAPITAL
    held_stocks = {}
    prev_date   = monthly_ends[0]
    nav_series  = {}
    cash_months = 0
    recording   = False
    in_months   = 0
    out_months  = 0

    for rebal_date in monthly_ends[1:]:
        idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
        if idx < 0: continue
        rebal_date = prices.index[idx]
        current_px = prices.iloc[idx]

        months_elapsed = (rebal_date - prev_date).days / 30.44
        cash_value    *= (1 + monthly_rate) ** months_elapsed

        nav = cash_value + sum(
            held_stocks[s] * current_px.get(s, 0)
            for s in held_stocks if not pd.isna(current_px.get(s, np.nan))
        )

        lb_idx = idx - LOOKBACK_DAYS
        if lb_idx < 0:
            prev_date = rebal_date
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        candidates  = returns_12m.nlargest(TOP_N).index.tolist() if market_up else []

        sell_value = cash_value
        for sym, shares in held_stocks.items():
            p = current_px.get(sym, np.nan)
            if not pd.isna(p):
                sell_value += shares * p * (1 - SLIPPAGE_PCT)
        held_stocks = {}
        cash_value  = sell_value

        if candidates:
            raw   = {s: max(returns_12m[s], 0.001) for s in candidates}
            total = sum(raw.values())
            weights = {s: v/total for s, v in raw.items()}
            invested = 0
            for sym, w in weights.items():
                p = current_px.get(sym, np.nan)
                if pd.isna(p) or p <= 0: continue
                cost = sell_value * w * (1 + SLIPPAGE_PCT)
                held_stocks[sym] = cost / p
                invested += cost
            cash_value = max(sell_value - invested, 0)

        # reset capital at start of recording window
        ts = pd.Timestamp(rebal_date)
        if date_filter_start and not recording:
            if ts >= pd.Timestamp(date_filter_start):
                recording  = True
                cash_value = CAPITAL  # reset to fresh 1M
                held_stocks = {}
                nav = CAPITAL
                nav_series[rebal_date] = CAPITAL

        in_window = True
        if date_filter_start and ts < pd.Timestamp(date_filter_start):
            in_window = False
        if date_filter_end and ts > pd.Timestamp(date_filter_end):
            in_window = False

        if in_window:
            nav_series[rebal_date] = nav
            if market_up:
                in_months += 1
            else:
                out_months += 1
                cash_months += 1

        prev_date = rebal_date

    final_nav = cash_value + sum(
        held_stocks[s] * prices[s].dropna().iloc[-1]
        for s in held_stocks if s in prices.columns
    )
    if nav_series:
        last_date = prices.index[-1]
        nav_series[last_date] = final_nav

    nav_s = pd.Series(nav_series)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")].sort_index()
    if len(nav_s) < 2:
        return None

    rm     = nav_s.pct_change().dropna()
    n_yrs  = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr   = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1/n_yrs) - 1
    sharpe = rm.mean() / rm.std() * np.sqrt(12) if rm.std() > 0 else 0
    dd_s   = (nav_s - nav_s.cummax()) / nav_s.cummax()
    max_dd = dd_s.min()
    calmar = cagr / abs(max_dd) if max_dd != 0 else 0
    sortino = rm.mean() / rm[rm<0].std() * np.sqrt(12) if len(rm[rm<0]) > 0 else 0
    win_rate = (rm > 0).mean() * 100

    nav_ye = nav_s.resample("YE").last()
    yr_rets = {}
    for i in range(1, len(nav_ye)):
        yr = nav_ye.index[i].year
        yr_rets[yr] = (nav_ye.iloc[i] / nav_ye.iloc[i-1] - 1) * 100

    return {
        "label":       label,
        "cagr":        cagr,
        "sharpe":      sharpe,
        "max_dd":      max_dd,
        "calmar":      calmar,
        "sortino":     sortino,
        "win_rate":    win_rate,
        "final_nav":   nav_s.iloc[-1],
        "cash_months": cash_months,
        "in_months":   in_months,
        "out_months":  out_months,
        "nav_series":  nav_s,
        "dd_series":   dd_s,
        "yr_rets":     yr_rets,
        "monthly_rets": rm,
    }


print("Running Full period  (2006–2026)...", end=" ", flush=True)
res_full = run_backtest("Full (2006–2026)")
print(f"CAGR={res_full['cagr']*100:.2f}%  Sharpe={res_full['sharpe']:.3f}  MaxDD={res_full['max_dd']*100:.1f}%")

print("Running In-Sample    (2006–2015)...", end=" ", flush=True)
res_is = run_backtest("In-Sample (2006–2015)", date_filter_end=IS_END)
print(f"CAGR={res_is['cagr']*100:.2f}%  Sharpe={res_is['sharpe']:.3f}  MaxDD={res_is['max_dd']*100:.1f}%")

print("Running Out-of-Sample(2016–2026)...", end=" ", flush=True)
res_oos = run_backtest("Out-of-Sample (2016–2026)", date_filter_start=OOS_START)
print(f"CAGR={res_oos['cagr']*100:.2f}%  Sharpe={res_oos['sharpe']:.3f}  MaxDD={res_oos['max_dd']*100:.1f}%")

results = [res_is, res_oos, res_full]

# ── summary table ─────────────────────────────────────────────────────────────
print("\n" + "=" * 76)
print("  IS / OOS VALIDATION — DualMom.Liq.Nifty50")
print("=" * 76)
metrics = [
    ("Period",            "2006–2015",                    "2016–2026",                    "2006–2026"),
    ("Years",             "10",                           "10.5",                         "20.5"),
    ("CAGR",              f"{res_is['cagr']*100:.2f}%",  f"{res_oos['cagr']*100:.2f}%",  f"{res_full['cagr']*100:.2f}%"),
    ("Sharpe Ratio",      f"{res_is['sharpe']:.3f}",     f"{res_oos['sharpe']:.3f}",     f"{res_full['sharpe']:.3f}"),
    ("Max Drawdown",      f"{res_is['max_dd']*100:.1f}%",f"{res_oos['max_dd']*100:.1f}%",f"{res_full['max_dd']*100:.1f}%"),
    ("Calmar Ratio",      f"{res_is['calmar']:.2f}",     f"{res_oos['calmar']:.2f}",     f"{res_full['calmar']:.2f}"),
    ("Sortino Ratio",     f"{res_is['sortino']:.2f}",    f"{res_oos['sortino']:.2f}",    f"{res_full['sortino']:.2f}"),
    ("Win Rate (months)", f"{res_is['win_rate']:.1f}%",  f"{res_oos['win_rate']:.1f}%",  f"{res_full['win_rate']:.1f}%"),
    ("Final NAV (Rs 10L)",f"Rs {res_is['final_nav']/1e6:.1f}L", f"Rs {res_oos['final_nav']/1e6:.1f}L", f"Rs {res_full['final_nav']/1e7:.1f}Cr"),
    ("Months IN",         str(res_is['in_months']),      str(res_oos['in_months']),      str(res_full['in_months'])),
    ("Months OUT",        str(res_is['out_months']),     str(res_oos['out_months']),     str(res_full['out_months'])),
]
print(f"  {'Metric':<22} {'In-Sample':>18} {'Out-of-Sample':>18} {'Full Period':>14}")
print(f"  {'-'*22} {'-'*18} {'-'*18} {'-'*14}")
for row in metrics:
    print(f"  {row[0]:<22} {row[1]:>18} {row[2]:>18} {row[3]:>14}")

print("\n  Verdict:")
cagr_gap = abs(res_is["cagr"] - res_oos["cagr"]) * 100
sharpe_gap = abs(res_is["sharpe"] - res_oos["sharpe"])
if cagr_gap < 5 and sharpe_gap < 0.3:
    print("  ROBUST — IS and OOS stats are closely aligned. No overfitting detected.")
elif cagr_gap < 10:
    print("  ACCEPTABLE — Moderate degradation in OOS. Typical for momentum strategies.")
else:
    print("  CAUTION — Large IS/OOS gap. Review parameter sensitivity.")

# ── year-by-year ──────────────────────────────────────────────────────────────
print("\n  Year-by-year returns vs Nifty:")
print(f"  {'Year':<6} {'Strategy':>10} {'Nifty':>8} {'Alpha':>8}  {'Period'}")
print(f"  {'----':<6} {'-'*10} {'-'*8} {'-'*8}  {'------'}")
all_years = sorted(set(res_full["yr_rets"].keys()))
for yr in all_years:
    ret    = res_full["yr_rets"].get(yr)
    nifty  = nifty_ann.get(yr)
    if ret is None or nifty is None: continue
    alpha  = ret - nifty
    period = "IS " if yr <= 2015 else "OOS"
    marker = " <-- OOS starts" if yr == 2016 else ""
    print(f"  {yr:<6} {ret:>9.1f}% {nifty:>7.1f}% {alpha:>+7.1f}%  {period}{marker}")

# ── chart ─────────────────────────────────────────────────────────────────────
print("\nGenerating chart...")
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10),
                                gridspec_kw={"height_ratios": [3, 1]},
                                sharex=True)
fig.patch.set_facecolor("#0F1923")
for ax in [ax1, ax2]:
    ax.set_facecolor("#0F1923")
    ax.tick_params(colors="#AAAAAA")
    for spine in ax.spines.values():
        spine.set_color("#333333")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

# IS period — blue, OOS period — orange
nav_full = res_full["nav_series"] / CAPITAL * 100
is_mask  = nav_full.index <= pd.Timestamp(IS_END)
oos_mask = nav_full.index >= pd.Timestamp(OOS_START)

ax1.plot(nav_full[is_mask].index,  nav_full[is_mask].values,
         color="#2196F3", linewidth=2.2, label=f"In-Sample 2006–2015  CAGR {res_is['cagr']*100:.1f}%  Sharpe {res_is['sharpe']:.2f}")
ax1.plot(nav_full[oos_mask].index, nav_full[oos_mask].values,
         color="#FF9800", linewidth=2.2, label=f"Out-of-Sample 2016–2026  CAGR {res_oos['cagr']*100:.1f}%  Sharpe {res_oos['sharpe']:.2f}")

# vertical divider
ax1.axvline(pd.Timestamp(OOS_START), color="#FFFFFF", linewidth=1.2,
            linestyle="--", alpha=0.5)
ax1.text(pd.Timestamp(OOS_START), ax1.get_ylim()[1] * 0.02,
         "  OOS →", color="white", fontsize=9, va="bottom")

ax1.set_ylabel("Portfolio Value (Indexed to 100)", color="#AAAAAA", fontsize=10)
ax1.set_title("DualMom.Liq.Nifty50 — In-Sample vs Out-of-Sample Validation  |  2006–2026",
              color="white", fontsize=13, fontweight="bold", pad=12)
ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
ax1.legend(loc="upper left", fontsize=9, framealpha=0.15,
           labelcolor="white", facecolor="#1C2B3A")
ax1.grid(axis="y", color="#222222", linewidth=0.5)
ax1.grid(axis="x", color="#1A1A1A", linewidth=0.5)

# shade OUT periods using vectorised comparison
nifty_ser  = nifty.copy()
ma_ser     = nifty_ma100.copy()
is_out     = (nifty_ser < ma_ser).reindex(nav_full.index, method="ffill").fillna(False)
in_out_chg = is_out.astype(int).diff().fillna(0)
out_starts = is_out.index[in_out_chg == 1].tolist()
out_ends   = is_out.index[in_out_chg == -1].tolist()
for os, oe in zip(out_starts, out_ends):
    ax1.axvspan(os, oe, alpha=0.08, color="gray", zorder=0)
    ax2.axvspan(os, oe, alpha=0.08, color="gray", zorder=0)

# drawdown
dd_full = res_full["dd_series"]
dd_is   = dd_full[dd_full.index <= pd.Timestamp(IS_END)]
dd_oos  = dd_full[dd_full.index >= pd.Timestamp(OOS_START)]
ax2.fill_between(dd_is.index,  dd_is.values*100,  0, alpha=0.4, color="#2196F3")
ax2.fill_between(dd_oos.index, dd_oos.values*100, 0, alpha=0.4, color="#FF9800")
ax2.plot(dd_is.index,  dd_is.values*100,  color="#2196F3", linewidth=1,
         label=f"IS MaxDD {res_is['max_dd']*100:.1f}%")
ax2.plot(dd_oos.index, dd_oos.values*100, color="#FF9800", linewidth=1,
         label=f"OOS MaxDD {res_oos['max_dd']*100:.1f}%")
ax2.axvline(pd.Timestamp(OOS_START), color="#FFFFFF", linewidth=1.2,
            linestyle="--", alpha=0.5)
ax2.set_ylabel("Drawdown %", color="#AAAAAA", fontsize=9)
ax2.set_xlabel("Year", color="#AAAAAA", fontsize=9)
ax2.legend(loc="lower left", fontsize=8, framealpha=0.15,
           labelcolor="white", facecolor="#1C2B3A")
ax2.grid(axis="y", color="#222222", linewidth=0.5)
ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))

note = "Gray bands = OUT (liquid fund)  |  Dashed line = IS/OOS split (Jan 2016)  |  Same parameters throughout — no re-tuning"
fig.text(0.5, 0.01, note, ha="center", fontsize=8, color="#666666")
plt.tight_layout(rect=[0, 0.02, 1, 1])

out_dir = Path(r"G:\fyers_data_pipeline\backtesting\book_strategies\antonacci\results")
out_dir.mkdir(exist_ok=True)
chart_path = out_dir / "is_oos_validation.png"
plt.savefig(chart_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close()
print(f"Chart saved → {chart_path}")
