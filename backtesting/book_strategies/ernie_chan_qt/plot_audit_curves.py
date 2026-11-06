"""Equity + drawdown curves for the 6-Nov-2026 StatArb.MR-first10 audit (reads results/audit_20261106/returns_*.csv)."""
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

def nav(label):
    r = pd.read_csv(D / f"returns_{label}.csv", index_col=0, parse_dates=True)["ret"].fillna(0)
    return (1 + r).cumprod(), r

def stats(r):
    n = (1 + r).cumprod(); yrs = (r.index[-1] - r.index[0]).days / 365.25
    return dict(cagr=(n.iloc[-1] ** (1 / yrs) - 1) * 100, dd=((n / n.cummax()) - 1).min() * 100,
                sh=r.mean() / r.std() * np.sqrt(252), mult=n.iloc[-1])

top = [("A_clean", "Original rules, clean data", C1),
       ("D_real_costs", "+ next-day fill, real costs", C2),
       ("E_tradeable_only", "+ only tradeable pairs", C3)]
bot = [("D_2021on_inSampleParams", "Original settings (picked with hindsight)", C2),
       ("WF_2021on_all", "Settings picked on 2015–20 only, all pairs", C4),
       ("WF_2021on_tradeable", "Settings picked on 2015–20 only, tradeable pairs", C3)]

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": GRID,
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2})
fig = plt.figure(figsize=(15, 13), facecolor=SURF)
gs = fig.add_gridspec(5, 1, height_ratios=[3, 1.1, 0.35, 3, 1.1], hspace=0.12)
ax1, ax1d = fig.add_subplot(gs[0]), fig.add_subplot(gs[1])
ax2, ax2d = fig.add_subplot(gs[3]), fig.add_subplot(gs[4])

short = {"A_clean": "Original rules, clean data", "D_real_costs": "+ next-day fill, real costs", "E_tradeable_only": "+ only tradeable pairs", "D_2021on_inSampleParams": "Hindsight settings", "WF_2021on_all": "No hindsight, all pairs", "WF_2021on_tradeable": "No hindsight, tradeable"}

def panel(ax, axd, runs, title, log, box_xy=(0.99, 0.03), box_va="bottom", box_ha="right"):
    rows = []
    for i, (lab, name, col) in enumerate(runs):
        n, r = nav(lab); s = stats(r)
        ax.plot(n.index, n.values, color=col, lw=2, label=name)
        dd = (n / n.cummax() - 1) * 100
        axd.plot(dd.index, dd.values, color=col, lw=1.4)
        ax.annotate(f"{s['cagr']:.1f}% a year", (n.index[-1], n.iloc[-1]), xytext=(6, [0, -9, 9][i] if not log else 0),
                    textcoords="offset points", va="center", fontsize=10, color=INK, fontweight="bold")
        rows.append((short.get(lab, name), s, col))
    if log: ax.set_yscale("log")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: (f"{v:,.0f}x" if log else f"{v:.1f}x")))
    ax.set_title(title, loc="left", fontsize=13, color=INK, pad=10)
    ax.set_ylabel("Growth of ₹1" + (" (log scale)" if log else ""))
    axd.set_ylabel("Drawdown %")
    axd.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    for a in (ax, axd):
        a.set_facecolor(SURF); a.grid(True, color=GRID, lw=0.6); a.set_axisbelow(True)
        for s in ("top", "right"): a.spines[s].set_visible(False)
        a.xaxis.set_major_locator(mdates.YearLocator()); a.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.tick_params(labelbottom=False)
    axd.axhline(0, color=INK2, lw=0.8)
    ax.legend(loc="upper left", frameon=False, fontsize=10)
    # stats box
    txt = "\n".join(f"{nm:28s}  CAGR {s['cagr']:5.1f}%   MaxDD {s['dd']:6.1f}%   Sharpe {s['sh']:.2f}" for nm, s, _ in rows)
    ax.text(*box_xy, txt, transform=ax.transAxes, ha=box_ha, va=box_va, family="DejaVu Sans Mono",
            fontsize=9, color=INK, bbox=dict(boxstyle="round,pad=0.5", fc="white", ec=GRID))
    ax.set_xlim(right=ax.get_xlim()[1] + 200); axd.set_xlim(ax.get_xlim())

panel(ax1, ax1d, top, "StatArb.MR-first10, 2015 → Nov-2026: each fix shrinks the result", True)
panel(ax2, ax2d, bot, "2021 → Nov-2026: original settings vs settings chosen without hindsight", False, box_xy=(0.99, 0.45), box_va="center", box_ha="right")
fig.text(0.01, 0.005, "Capital split equally per pair, each slice used as futures margin (SPAN 15%). Compounding. "
         "Half-size positions ≈ half the CAGR and drawdown.  Source: audit_rerun_20261106.py",
         fontsize=9, color=INK2)
out = D / "equity_drawdown_audit.png"
fig.savefig(out, dpi=130, bbox_inches="tight", facecolor=SURF)
print(out)
