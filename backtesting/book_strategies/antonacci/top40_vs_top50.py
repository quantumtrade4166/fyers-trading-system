"""
DualMom — Top 40 vs Top 50 comparison
Everything identical except TOP_N: 50 vs 40
Outputs: stats table, year-by-year, equity curve PNG
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
CAPITAL        = 1_000_000
SLIPPAGE_PCT   = 0.001
LIQUID_FUND_PA = 0.06
START_DATE     = "2006-01-01"
END_DATE       = "2026-06-18"

print("Downloading Nifty 50...")
nifty_raw = yf.download("^NSEI", start="2005-01-01", end=END_DATE, auto_adjust=True, progress=False)
nifty     = nifty_raw["Close"].squeeze()
nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
nifty_ma100 = nifty.rolling(100).mean()

print("Loading 500 symbols...")
frames = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close"])
    df.index = pd.to_datetime(df.index)
    frames[f.stem] = df["close"]
prices = pd.DataFrame(frames).sort_index().loc[START_DATE:END_DATE]
monthly_ends = prices.resample("ME").last().index
monthly_rate = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Matrix: {prices.shape}\n")


def run_backtest(top_n, label):
    cash_value  = CAPITAL
    held_stocks = {}
    prev_date   = monthly_ends[0]
    nav_series  = {monthly_ends[0]: CAPITAL}
    cash_months = 0

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
            nav_series[rebal_date] = nav
            prev_date = rebal_date
            continue

        nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
        n_ma      = nifty_ma100.iloc[nifty_idx]
        n_px      = nifty.iloc[nifty_idx]
        market_up = (not pd.isna(n_ma)) and (n_px > n_ma)

        if not market_up:
            cash_months += 1

        past_px     = prices.iloc[lb_idx]
        returns_12m = (current_px / past_px - 1).dropna()
        candidates  = returns_12m.nlargest(top_n).index.tolist() if market_up else []

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

        nav_series[rebal_date] = nav
        prev_date = rebal_date

    final_nav = cash_value + sum(
        held_stocks[s] * prices[s].dropna().iloc[-1]
        for s in held_stocks if s in prices.columns
    )
    nav_s = pd.Series(nav_series)
    nav_s = nav_s[~nav_s.index.duplicated(keep="last")].sort_index()
    nav_s[prices.index[-1]] = final_nav

    rm     = nav_s.pct_change().dropna()
    n_yrs  = (nav_s.index[-1] - nav_s.index[0]).days / 365.25
    cagr   = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1/n_yrs) - 1
    sharpe = rm.mean() / rm.std() * np.sqrt(12)
    dd_s   = (nav_s - nav_s.cummax()) / nav_s.cummax()
    max_dd = dd_s.min()

    # rolling 12m return
    nav_ye = nav_s.resample("YE").last()
    yr_rets = {}
    for i in range(1, len(nav_ye)):
        yr = nav_ye.index[i].year
        yr_rets[yr] = (nav_ye.iloc[i] / nav_ye.iloc[i-1] - 1) * 100

    print(f"  {label}: CAGR={cagr*100:.2f}%  Sharpe={sharpe:.3f}  MaxDD={max_dd*100:.1f}%  NAV=Rs {nav_s.iloc[-1]/1e7:.1f}Cr  CashMths={cash_months}")

    return {
        "label":       label,
        "top_n":       top_n,
        "cagr":        cagr,
        "sharpe":      sharpe,
        "max_dd":      max_dd,
        "final_nav":   nav_s.iloc[-1],
        "cash_months": cash_months,
        "nav_series":  nav_s,
        "dd_series":   dd_s,
        "yr_rets":     yr_rets,
        "monthly_rets": rm,
    }


print("Running backtests...")
res50 = run_backtest(50, "Top 50 (baseline)")
res40 = run_backtest(40, "Top 40")

results = [res50, res40]

# ── additional stats ───────────────────────────────────────────────────────────
def extra_stats(r):
    rm = r["monthly_rets"]
    pos = rm[rm > 0]
    neg = rm[rm < 0]
    return {
        "win_rate":     len(pos) / len(rm) * 100,
        "avg_up":       pos.mean() * 100,
        "avg_dn":       neg.mean() * 100,
        "best_month":   rm.max() * 100,
        "worst_month":  rm.min() * 100,
        "calmar":       r["cagr"] / abs(r["max_dd"]) if r["max_dd"] != 0 else 0,
        "sortino":      rm.mean() / rm[rm < 0].std() * np.sqrt(12) if len(rm[rm < 0]) > 0 else 0,
    }

ex50 = extra_stats(res50)
ex40 = extra_stats(res40)

# ── print summary ──────────────────────────────────────────────────────────────
print("\n" + "=" * 72)
print("  TOP 50 vs TOP 40 — FULL STATS COMPARISON")
print("=" * 72)
metrics = [
    ("CAGR",              f"{res50['cagr']*100:.2f}%",       f"{res40['cagr']*100:.2f}%"),
    ("Sharpe Ratio",      f"{res50['sharpe']:.3f}",           f"{res40['sharpe']:.3f}"),
    ("Max Drawdown",      f"{res50['max_dd']*100:.1f}%",      f"{res40['max_dd']*100:.1f}%"),
    ("Calmar Ratio",      f"{ex50['calmar']:.2f}",            f"{ex40['calmar']:.2f}"),
    ("Sortino Ratio",     f"{ex50['sortino']:.2f}",           f"{ex40['sortino']:.2f}"),
    ("Final NAV",         f"Rs {res50['final_nav']/1e7:.1f}Cr", f"Rs {res40['final_nav']/1e7:.1f}Cr"),
    ("Win Rate (months)", f"{ex50['win_rate']:.1f}%",         f"{ex40['win_rate']:.1f}%"),
    ("Avg Up Month",      f"{ex50['avg_up']:.2f}%",           f"{ex40['avg_up']:.2f}%"),
    ("Avg Down Month",    f"{ex50['avg_dn']:.2f}%",           f"{ex40['avg_dn']:.2f}%"),
    ("Best Month",        f"{ex50['best_month']:.2f}%",       f"{ex40['best_month']:.2f}%"),
    ("Worst Month",       f"{ex50['worst_month']:.2f}%",      f"{ex40['worst_month']:.2f}%"),
    ("Cash Months (OUT)", f"{res50['cash_months']}",          f"{res40['cash_months']}"),
]
print(f"  {'Metric':<22} {'Top 50':>14} {'Top 40':>14}  {'Winner'}")
print(f"  {'-'*22} {'-'*14} {'-'*14}  {'-'*10}")
for metric, v50, v40 in metrics:
    # determine winner (higher is better except MaxDD, AvgDown, WorstMonth)
    try:
        n50 = float(v50.replace("%","").replace("Rs","").replace("Cr","").strip())
        n40 = float(v40.replace("%","").replace("Rs","").replace("Cr","").strip())
        lower_better = metric in ("Max Drawdown", "Avg Down Month", "Worst Month")
        if lower_better:
            winner = "Top 40 ✓" if n40 < n50 else ("Top 50 ✓" if n50 < n40 else "Tie")
        else:
            winner = "Top 40 ✓" if n40 > n50 else ("Top 50 ✓" if n50 > n40 else "Tie")
    except:
        winner = ""
    print(f"  {metric:<22} {v50:>14} {v40:>14}  {winner}")

print("\n  Year-by-year returns:")
print(f"  {'Year':<6} {'Top 50':>10} {'Top 40':>10} {'Diff':>8}  Winner")
print(f"  {'----':<6} {'-'*10} {'-'*10} {'-'*8}  ------")
all_years = sorted(set(res50["yr_rets"]) | set(res40["yr_rets"]))
for yr in all_years:
    r50 = res50["yr_rets"].get(yr)
    r40 = res40["yr_rets"].get(yr)
    if r50 is None or r40 is None: continue
    diff   = r40 - r50
    winner = "Top40 ✓" if r40 > r50 else "Top50 ✓"
    print(f"  {yr:<6} {r50:>9.1f}% {r40:>9.1f}% {diff:>+7.1f}%  {winner}")

# ── equity curve + drawdown chart ─────────────────────────────────────────────
print("\nGenerating chart...")

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10),
                                gridspec_kw={"height_ratios": [3, 1]},
                                sharex=True)
fig.patch.set_facecolor("#0F1923")
for ax in [ax1, ax2]:
    ax.set_facecolor("#0F1923")
    ax.tick_params(colors="#AAAAAA")
    ax.spines["bottom"].set_color("#333333")
    ax.spines["left"].set_color("#333333")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

colors = {"Top 50 (baseline)": "#2196F3", "Top 40": "#FF9800"}
labels_short = {"Top 50 (baseline)": "Top 50", "Top 40": "Top 40"}

# NAV curves
for r in results:
    nav = r["nav_series"] / CAPITAL * 100  # index to 100
    ax1.plot(nav.index, nav.values, color=colors[r["label"]],
             linewidth=2, label=f"{labels_short[r['label']]}  CAGR {r['cagr']*100:.2f}%  NAV Rs {r['final_nav']/1e7:.1f}Cr")

ax1.set_ylabel("Portfolio Value (Indexed to 100)", color="#AAAAAA", fontsize=10)
ax1.set_title("DualMom.Liq.Nifty50 — Top 50 vs Top 40  |  2006–2026",
              color="white", fontsize=13, fontweight="bold", pad=12)
ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
ax1.legend(loc="upper left", fontsize=9, framealpha=0.15,
           labelcolor="white", facecolor="#1C2B3A")
ax1.grid(axis="y", color="#222222", linewidth=0.5)
ax1.grid(axis="x", color="#1A1A1A", linewidth=0.5)

# shade OUT periods (liquid fund) using Top 50 signal
nav50 = res50["nav_series"]
monthly_rate_ = (1 + LIQUID_FUND_PA) ** (1/12) - 1
prev_sig = None
out_start = None
for rebal_date in monthly_ends[1:]:
    idx = prices.index.get_indexer([rebal_date], method="ffill")[0]
    if idx < 0: continue
    rebal_date = prices.index[idx]
    lb_idx = idx - LOOKBACK_DAYS
    if lb_idx < 0: continue
    nifty_idx = nifty.index.get_indexer([rebal_date], method="ffill")[0]
    n_ma = nifty_ma100.iloc[nifty_idx]
    n_px = nifty.iloc[nifty_idx]
    if pd.isna(n_ma): continue
    sig = "IN" if n_px > n_ma else "OUT"
    if prev_sig == "IN" and sig == "OUT":
        out_start = rebal_date
    elif prev_sig == "OUT" and sig == "IN" and out_start:
        ax1.axvspan(out_start, rebal_date, alpha=0.08, color="gray", zorder=0)
        ax2.axvspan(out_start, rebal_date, alpha=0.08, color="gray", zorder=0)
        out_start = None
    prev_sig = sig

# drawdown
for r in results:
    ax2.fill_between(r["dd_series"].index, r["dd_series"].values * 100, 0,
                     alpha=0.4, color=colors[r["label"]])
    ax2.plot(r["dd_series"].index, r["dd_series"].values * 100,
             color=colors[r["label"]], linewidth=1,
             label=f"{labels_short[r['label']]}  MaxDD {r['max_dd']*100:.1f}%")

ax2.set_ylabel("Drawdown %", color="#AAAAAA", fontsize=9)
ax2.set_xlabel("Year", color="#AAAAAA", fontsize=9)
ax2.legend(loc="lower left", fontsize=8, framealpha=0.15,
           labelcolor="white", facecolor="#1C2B3A")
ax2.grid(axis="y", color="#222222", linewidth=0.5)
ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))

# annotation for max DDs
for r in results:
    dd = r["dd_series"]
    worst_dt = dd.idxmin()
    ax2.annotate(f"{r['max_dd']*100:.1f}%",
                 xy=(worst_dt, r["max_dd"]*100),
                 xytext=(0, -15), textcoords="offset points",
                 color=colors[r["label"]], fontsize=7,
                 arrowprops=dict(arrowstyle="-", color=colors[r["label"]], lw=0.8))

note = "Gray bands = OUT (liquid fund)  |  Momentum weighted allocation  |  0.1% slippage/side"
fig.text(0.5, 0.01, note, ha="center", fontsize=8, color="#666666")

plt.tight_layout(rect=[0, 0.02, 1, 1])

out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
chart_path = out_dir / "top40_vs_top50_equity.png"
plt.savefig(chart_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close()
print(f"Chart saved → {chart_path}")

pd.DataFrame({
    "Top50_NAV": res50["nav_series"],
    "Top40_NAV": res40["nav_series"],
}).to_csv(out_dir / "top40_vs_top50_nav.csv")
print(f"NAV CSV saved → {out_dir / 'top40_vs_top50_nav.csv'}")
