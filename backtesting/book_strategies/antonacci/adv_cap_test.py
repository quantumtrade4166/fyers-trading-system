"""
DualMom Top 40 — ADV cap comparison
Tests baseline (no cap) vs 1%, 2%, 3% of 20-day ADV per position
ADV = 20-day average of (close * volume) in rupees
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
ADV_WINDOW     = 20
TOP_N          = 40
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

print("Loading 500 symbols (close + volume)...")
close_frames  = {}
adv_frames    = {}
for f in DATA_DIR.glob("*.parquet"):
    df = pd.read_parquet(f, columns=["close", "volume"])
    df.index = pd.to_datetime(df.index)
    close_frames[f.stem] = df["close"]
    adv_frames[f.stem]   = df["close"] * df["volume"]   # daily traded value in Rs

prices   = pd.DataFrame(close_frames).sort_index().loc[START_DATE:END_DATE]
adv_df   = pd.DataFrame(adv_frames).sort_index().loc[START_DATE:END_DATE]
adv20    = adv_df.rolling(ADV_WINDOW).mean()             # 20-day avg daily traded value

monthly_ends  = prices.resample("ME").last().index
monthly_rate  = (1 + LIQUID_FUND_PA) ** (1/12) - 1
print(f"Matrix: {prices.shape}\n")


def run_backtest(adv_cap_pct, label):
    """
    adv_cap_pct: max fraction of 20-day ADV we can put in one stock.
                 None = no cap (baseline).
    """
    cash_value  = CAPITAL
    held_stocks = {}
    prev_date   = monthly_ends[0]
    nav_series  = {monthly_ends[0]: CAPITAL}
    cash_months = 0
    capped_events = 0   # count how many times a position was capped

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

            # ── ADV cap: trim weights where position > adv_cap * ADV20 ────────
            if adv_cap_pct is not None:
                # get 20-day ADV at rebal date for each candidate
                adv_idx = adv20.index.get_indexer([rebal_date], method="ffill")[0]
                if adv_idx >= 0:
                    adv_row = adv20.iloc[adv_idx]
                    max_rs  = {s: adv_row.get(s, np.nan) * adv_cap_pct for s in candidates}

                    # iterative renormalization: cap → renorm → repeat
                    for _ in range(10):   # converges in 2-3 passes
                        new_weights = dict(weights)
                        overflow    = 0.0
                        uncapped    = []
                        for s in candidates:
                            cap_rs  = max_rs.get(s, np.nan)
                            alloc   = sell_value * new_weights[s]
                            if not pd.isna(cap_rs) and alloc > cap_rs:
                                overflow += alloc - cap_rs
                                new_weights[s] = cap_rs / sell_value
                                capped_events += 1
                            else:
                                uncapped.append(s)
                        if overflow < 1 or not uncapped:
                            break
                        # distribute overflow proportionally to uncapped
                        unc_total = sum(new_weights[s] for s in uncapped)
                        if unc_total <= 0:
                            break
                        for s in uncapped:
                            new_weights[s] += (new_weights[s] / unc_total) * (overflow / sell_value)
                        weights = new_weights

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
    calmar = cagr / abs(max_dd) if max_dd != 0 else 0

    nav_ye = nav_s.resample("YE").last()
    yr_rets = {}
    for i in range(1, len(nav_ye)):
        yr = nav_ye.index[i].year
        yr_rets[yr] = (nav_ye.iloc[i] / nav_ye.iloc[i-1] - 1) * 100

    print(f"  {label}: CAGR={cagr*100:.2f}%  Sharpe={sharpe:.3f}  MaxDD={max_dd*100:.1f}%  "
          f"NAV=Rs {nav_s.iloc[-1]/1e7:.2f}Cr  CappedEvents={capped_events}")

    return {
        "label": label, "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
        "calmar": calmar, "final_nav": nav_s.iloc[-1],
        "nav_series": nav_s, "dd_series": dd_s, "yr_rets": yr_rets,
        "monthly_rets": rm, "capped_events": capped_events,
    }


print("Running backtests...")
configs = [
    (None,  "No cap (baseline)"),
    (0.01,  "1% ADV cap"),
    (0.02,  "2% ADV cap"),
    (0.03,  "3% ADV cap"),
]
results = [run_backtest(cap, lbl) for cap, lbl in configs]

# ── summary table ──────────────────────────────────────────────────────────────
print("\n" + "=" * 80)
print("  TOP 40 — ADV CAP COMPARISON")
print("=" * 80)
hdr = f"  {'Metric':<22}"
for r in results:
    hdr += f"  {r['label']:>18}"
print(hdr)
print("  " + "-" * (22 + 20 * len(results)))

rows = [
    ("CAGR",          lambda r: f"{r['cagr']*100:.2f}%"),
    ("Sharpe",        lambda r: f"{r['sharpe']:.3f}"),
    ("Max Drawdown",  lambda r: f"{r['max_dd']*100:.1f}%"),
    ("Calmar",        lambda r: f"{r['calmar']:.2f}"),
    ("Final NAV",     lambda r: f"Rs {r['final_nav']/1e7:.2f}Cr"),
    ("Capped events", lambda r: f"{r['capped_events']}"),
]
for name, fn in rows:
    line = f"  {name:<22}"
    for r in results:
        line += f"  {fn(r):>18}"
    print(line)

print("\n  Year-by-year returns:")
hdr2 = f"  {'Year':<6}"
for r in results:
    hdr2 += f"  {r['label']:>18}"
print(hdr2)
print("  " + "-" * (6 + 20 * len(results)))
all_years = sorted(set().union(*[r["yr_rets"].keys() for r in results]))
for yr in all_years:
    line = f"  {yr:<6}"
    vals = []
    for r in results:
        v = r["yr_rets"].get(yr)
        s = f"{v:+.1f}%" if v is not None else "  —"
        line += f"  {s:>18}"
        vals.append(v)
    # mark best year
    valid = [v for v in vals if v is not None]
    if valid:
        best = max(valid)
        line += "  ← " + results[vals.index(best)]["label"] if best == max(valid) else ""
    print(line)

# ── equity curve chart ─────────────────────────────────────────────────────────
print("\nGenerating chart...")
colors = ["#AAAAAA", "#2196F3", "#4CAF50", "#FF9800"]

fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 10),
                                gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
fig.patch.set_facecolor("#0F1923")
for ax in [ax1, ax2]:
    ax.set_facecolor("#0F1923")
    ax.tick_params(colors="#AAAAAA")
    for sp in ["bottom", "left"]:
        ax.spines[sp].set_color("#333333")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

for r, c in zip(results, colors):
    lw = 2.5 if "baseline" in r["label"] else 1.5
    nav = r["nav_series"] / CAPITAL * 100
    ax1.plot(nav.index, nav.values, color=c, linewidth=lw,
             label=f"{r['label']}  CAGR {r['cagr']*100:.2f}%  NAV Rs {r['final_nav']/1e7:.2f}Cr")
    ax2.plot(r["dd_series"].index, r["dd_series"].values * 100, color=c, linewidth=1.2)

ax1.set_title("DualMom.Liq.Nifty50 Top 40 — ADV Cap Comparison  |  2006–2026",
              color="white", fontsize=13, fontweight="bold", pad=12)
ax1.set_ylabel("Portfolio Value (Indexed to 100)", color="#AAAAAA", fontsize=10)
ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
ax1.legend(loc="upper left", fontsize=9, framealpha=0.15,
           labelcolor="white", facecolor="#1C2B3A")
ax1.grid(axis="y", color="#222222", linewidth=0.5)
ax1.grid(axis="x", color="#1A1A1A", linewidth=0.5)

ax2.set_ylabel("Drawdown %", color="#AAAAAA", fontsize=9)
ax2.set_xlabel("Year", color="#AAAAAA", fontsize=9)
ax2.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:.0f}%"))
ax2.grid(axis="y", color="#222222", linewidth=0.5)

note = f"ADV cap = max % of 20-day avg daily traded value per position  |  Top 40 momentum weighted  |  0.1% slippage/side"
fig.text(0.5, 0.01, note, ha="center", fontsize=8, color="#666666")

plt.tight_layout(rect=[0, 0.02, 1, 1])
out_dir = Path(__file__).parent / "results"
out_dir.mkdir(exist_ok=True)
chart_path = out_dir / "adv_cap_comparison.png"
plt.savefig(chart_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close()
print(f"Chart saved → {chart_path}")
