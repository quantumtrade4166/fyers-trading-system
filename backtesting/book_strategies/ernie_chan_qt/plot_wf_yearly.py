"""Equity + drawdown for the yearly re-calibration walk-forward (reads results/audit_20261106/returns_WFY_*.csv)."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

D = Path(__file__).resolve().parent / "results" / "audit_20261106"
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
C1, C2, C3, C4 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
RUNS = [("WFY_expanding_all", "Yearly recal, all history, all 10 pairs", C1),
        ("WFY_expanding_tradeable", "Yearly recal, all history, 8 tradeable pairs", C3),
        ("WFY_rolling3_tradeable", "Yearly recal, last 3 yrs only, 8 tradeable pairs", C4),
        ("E_tradeable_only", "Reference: fixed hindsight settings, 8 pairs", C2)]
START = pd.Timestamp("2017-01-01")


def stats(r):
    n = (1 + r).cumprod(); yrs = (r.index[-1] - r.index[0]).days / 365.25
    return dict(cagr=(n.iloc[-1] ** (1 / yrs) - 1) * 100, dd=(n / n.cummax() - 1).min() * 100,
                sh=r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0)


plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
fig, (ax, axd) = plt.subplots(2, 1, figsize=(14, 8.5), facecolor=SURF, sharex=True,
                              gridspec_kw=dict(height_ratios=[3, 1.2], hspace=0.08))
rows = []
for lab, name, col in RUNS:
    f = D / f"returns_{lab}.csv"
    if not f.exists(): continue
    r = pd.read_csv(f, index_col=0, parse_dates=True)["ret"].fillna(0)
    r = r[r.index >= START]
    n = (1 + r).cumprod(); s = stats(r)
    ls = "--" if lab.startswith("E_") else "-"
    ax.plot(n.index, n.values, color=col, lw=2, ls=ls, label=name)
    axd.plot(n.index, (n / n.cummax() - 1) * 100, color=col, lw=1.3, ls=ls)
    rows.append((name, s))
for a in (ax, axd):
    a.set_facecolor(SURF); a.grid(True, color=GRID, lw=0.6); a.set_axisbelow(True)
    for sp in ("top", "right"): a.spines[sp].set_visible(False)
axd.xaxis.set_major_locator(mdates.YearLocator()); axd.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.1f}x"))
axd.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
ax.axhline(1, color=INK2, lw=0.8); axd.axhline(0, color=INK2, lw=0.8)
ax.set_ylabel("Growth of ₹1"); axd.set_ylabel("Drawdown %")
ax.set_title("StatArb.MR-first10 — settings re-picked every January from past data only (2017 → Nov-2026)",
             loc="left", fontsize=13, color=INK, pad=10)
ax.legend(loc="upper left", frameon=False, fontsize=10)
txt = "\n".join(f"{nm:48s}  CAGR {s['cagr']:5.1f}%  MaxDD {s['dd']:6.1f}%  Sharpe {s['sh']:.2f}" for nm, s in rows)
ax.text(0.01, 0.70, txt, transform=ax.transAxes, ha="left", va="top", family="DejaVu Sans Mono", fontsize=8.5,
        color=INK, bbox=dict(boxstyle="round,pad=0.5", fc="white", ec=GRID))
fig.text(0.01, 0.005, "Next-day open fills, 0.05%/side + 0.10%/month roll. Equal capital slice per pair used as futures margin "
         "(SPAN 15%), compounding. Half-size ≈ half the CAGR and drawdown.", fontsize=9, color=INK2)
out = D / "equity_drawdown_wf_yearly.png"
fig.savefig(out, dpi=130, bbox_inches="tight", facecolor=SURF)
print(out)
