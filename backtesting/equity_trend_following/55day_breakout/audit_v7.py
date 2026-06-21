import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
STARTING_CAPITAL = 10_00_000

def load_eq(tag): return pd.read_csv(RESULTS_DIR / f"equity_55day_{tag}.csv", index_col=0, parse_dates=True)["equity"]
def load_tr(tag): return pd.read_csv(RESULTS_DIR / f"trades_55day_{tag}.csv", parse_dates=["entry_date", "exit_date"])
def drawdown(eq): pk = eq.cummax(); return (eq - pk) / pk * 100


# ── AUDIT ─────────────────────────────────────────────────────
print("=" * 65)
print("  AUDIT — V7a & V7b")
print("=" * 65)

for tag, label in [("v7a", "V7a"), ("v7b", "V7b")]:
    eq = load_eq(tag)
    tr = load_tr(tag)

    print(f"\n── {label} ──────────────────────────────────────────")
    print(f"  Final equity : ₹{eq.iloc[-1]:,.0f}")
    print(f"  Max equity   : ₹{eq.max():,.0f}  on {eq.idxmax().date()}")
    print(f"  Max DD       : {drawdown(eq).min():.2f}%")

    # Sanity check: max possible daily equity
    # 100 positions × ₹10K = ₹10L deployed + cash
    # Equity can only grow via trade profits, not magic
    print(f"\n  Equity on key dates:")
    for d in ["2009-12-31", "2010-01-04", "2010-02-06", "2010-02-08", "2010-03-31", "2010-06-30", "2010-12-31"]:
        try:
            idx = pd.Timestamp(d)
            # find nearest date
            nearest = eq.index[eq.index.get_indexer([idx], method="nearest")[0]]
            print(f"    {str(nearest.date())}: ₹{eq.loc[nearest]:>15,.0f}")
        except:
            pass

    # Check portfolio stop trades
    if "exit_reason" in tr.columns:
        stops = tr[tr["exit_reason"] == "portfolio_stop"]
        print(f"\n  Portfolio stop trades: {len(stops)}")
        if not stops.empty:
            print(f"  {'Exit Date':<12} {'Symbol':<15} {'Entry':>10} {'Exit':>10} {'PNL':>10}")
            print("  " + "-"*60)
            for _, row in stops.iterrows():
                print(f"  {str(row['exit_date'].date()):<12} {row['symbol']:<15} "
                      f"₹{row['entry_price']:>8,.1f} ₹{row['exit_price']:>8,.1f} ₹{row['pnl']:>+8,.0f}")

    # Check trades around 2010-02-06 to 2010-03-31
    print(f"\n  Trades entered between 2010-02-06 and 2010-04-01:")
    mask = (tr["entry_date"] >= "2010-02-06") & (tr["entry_date"] <= "2010-04-01")
    chunk = tr[mask].sort_values("entry_date")
    print(f"  Count: {len(chunk)}")
    if not chunk.empty:
        print(f"  {'Entry Date':<12} {'Symbol':<15} {'Entry Px':>10} {'Exit Px':>10} {'PNL':>10} {'Ret%':>8}")
        print("  " + "-"*65)
        for _, row in chunk.head(20).iterrows():
            print(f"  {str(row['entry_date'].date()):<12} {row['symbol']:<15} "
                  f"₹{row['entry_price']:>8,.1f} ₹{row['exit_price']:>8,.1f} "
                  f"₹{row['pnl']:>+8,.0f} {row['return_pct']:>+7.1f}%")

    # Biggest winning trades overall
    print(f"\n  Top 10 biggest winning trades (all time):")
    top = tr.nlargest(10, "pnl")[["symbol","entry_date","exit_date","entry_price","exit_price","pnl","return_pct"]]
    for _, row in top.iterrows():
        print(f"  {str(row['entry_date'].date())} → {str(row['exit_date'].date())}  "
              f"{row['symbol']:<15} pnl=₹{row['pnl']:>+8,.0f}  ret={row['return_pct']:>+6.1f}%")

print()

# ── PLOTS ─────────────────────────────────────────────────────
fig, axes = plt.subplots(2, 2, figsize=(18, 10))
fig.suptitle("55-Day Breakout — V7a & V7b: Equity & Drawdown", fontsize=14, fontweight="bold")

configs = [
    ("v7a", "V7a — Portfolio DD Stop −15%",           "#2196F3", "#F44336", axes[0][0], axes[1][0]),
    ("v7b", "V7b — Portfolio DD Stop −15% + Nifty MA","#4CAF50", "#FF9800", axes[0][1], axes[1][1]),
]

for tag, label, ec, dc, ax_eq, ax_dd in configs:
    eq = load_eq(tag)
    dd = drawdown(eq)

    # Equity
    ax_eq.plot(eq.index, eq / 1e5, color=ec, linewidth=1.2)
    ax_eq.set_title(label, fontweight="bold", fontsize=10)
    ax_eq.set_ylabel("Equity (₹ Lakhs)")
    ax_eq.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:.0f}L"))
    ax_eq.axhline(10, color="gray", linestyle="--", linewidth=0.8, label="Start ₹10L")
    ax_eq.grid(True, alpha=0.3)
    ax_eq.legend(fontsize=8)

    # Drawdown
    ax_dd.fill_between(dd.index, dd, 0, color=dc, alpha=0.5)
    ax_dd.plot(dd.index, dd, color=dc, linewidth=0.8)
    ax_dd.set_title(f"{label.split('—')[0].strip()} — Drawdown", fontweight="bold", fontsize=10)
    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.axhline(dd.min(), color="darkred", linestyle="--", linewidth=0.8,
                  label=f"Max DD {dd.min():.1f}%")
    ax_dd.grid(True, alpha=0.3)
    ax_dd.legend(fontsize=8)

plt.tight_layout()
out = RESULTS_DIR / "equity_drawdown_v7.png"
plt.savefig(out, dpi=150, bbox_inches="tight")
print(f"\nChart saved → {out}")
plt.show()
