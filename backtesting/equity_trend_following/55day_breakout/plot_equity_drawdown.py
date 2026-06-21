import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"

def load_equity(filename: str) -> pd.DataFrame:
    df = pd.read_csv(RESULTS_DIR / filename, index_col=0, parse_dates=True)
    return df

def compute_drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return (equity - peak) / peak * 100

def plot_all():
    v1 = load_equity("equity_55day_v1.csv")["equity"]
    v2 = load_equity("equity_55day_v2.csv")["equity"]

    dd1 = compute_drawdown(v1)
    dd2 = compute_drawdown(v2)

    fig, axes = plt.subplots(2, 2, figsize=(18, 10))
    fig.suptitle("55-Day Breakout Strategy — Equity & Drawdown", fontsize=15, fontweight="bold")

    # ── V1 Equity ─────────────────────────────────────────────
    ax = axes[0][0]
    ax.plot(v1.index, v1 / 1e5, color="#2196F3", linewidth=1.5)
    ax.set_title("V1 — No Filter — Equity Curve", fontweight="bold")
    ax.set_ylabel("Equity (₹ Lakhs)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:.0f}L"))
    ax.axhline(10, color="gray", linestyle="--", linewidth=0.8, label="Start ₹10L")
    ax.fill_between(v1.index, v1 / 1e5, 10, where=(v1 / 1e5 >= 10), alpha=0.15, color="#2196F3")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── V2 Equity ─────────────────────────────────────────────
    ax = axes[0][1]
    ax.plot(v2.index, v2 / 1e5, color="#4CAF50", linewidth=1.5)
    ax.set_title("V2 — Nifty 200MA Filter — Equity Curve", fontweight="bold")
    ax.set_ylabel("Equity (₹ Lakhs)")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"₹{x:.0f}L"))
    ax.axhline(10, color="gray", linestyle="--", linewidth=0.8, label="Start ₹10L")
    ax.fill_between(v2.index, v2 / 1e5, 10, where=(v2 / 1e5 >= 10), alpha=0.15, color="#4CAF50")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── V1 Drawdown ───────────────────────────────────────────
    ax = axes[1][0]
    ax.fill_between(dd1.index, dd1, 0, color="#F44336", alpha=0.6)
    ax.plot(dd1.index, dd1, color="#F44336", linewidth=0.8)
    ax.set_title("V1 — Drawdown", fontweight="bold")
    ax.set_ylabel("Drawdown (%)")
    ax.axhline(-71.12, color="darkred", linestyle="--", linewidth=0.8, label="Max DD −71.12%")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    # ── V2 Drawdown ───────────────────────────────────────────
    ax = axes[1][1]
    ax.fill_between(dd2.index, dd2, 0, color="#FF9800", alpha=0.6)
    ax.plot(dd2.index, dd2, color="#FF9800", linewidth=0.8)
    ax.set_title("V2 — Nifty 200MA Filter — Drawdown", fontweight="bold")
    ax.set_ylabel("Drawdown (%)")
    ax.axhline(-53.12, color="darkorange", linestyle="--", linewidth=0.8, label="Max DD −53.12%")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = RESULTS_DIR / "equity_drawdown_v1_v2.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Chart saved → {out_path}")
    plt.show()

if __name__ == "__main__":
    plot_all()
