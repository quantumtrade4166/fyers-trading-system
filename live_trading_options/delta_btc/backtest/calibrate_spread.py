"""
backtest/calibrate_spread.py — measure real BTC option spreads from our archive.
================================================================================

The downloaded history has no bid/ask, so the backtest has to MODEL the spread.
The first model — max($2, 5% of mark) — came from the delta-neutral backtest and
was deliberately pessimistic. Comparing backtests with live fills (16-19 Sep)
showed where it hurts: at the 17:10 square-off a near-worthless leg really cost
0.40 to buy back, and the model charged 2.20 because of the $2 floor.

This measures the real thing: every minute of every contract in
data/chain_archive/, bid-ask spread in DOLLARS, grouped by the mark it sat on.
Writes config/spread_model.json, which backtest/vwap_history.py reads:

    {"edges": [...mark bucket edges...], "spread": [...median $ spread per bucket...],
     "p75": [...], "n": [...]}

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\backtest\\calibrate_spread.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import glob
import json

import numpy as np
import pandas as pd

EDGES = [0, 1, 2, 5, 10, 20, 35, 50, 75, 100, 150, 200, 300, 500, 1000, 1e9]
STEP = 3   # every 3rd chunk is plenty (millions of rows) and keeps memory flat


def main():
    files = sorted(glob.glob(str(ROOT / "data" / "chain_archive" / "*" / "chunk_*.parquet")))
    parts = []
    for f in files[::STEP]:
        try:
            x = pd.read_parquet(f, columns=["mark", "best_bid", "best_ask"])
        except Exception:
            continue
        x = x.dropna()
        x = x[(x["best_bid"] > 0) & (x["best_ask"] > x["best_bid"]) & (x["mark"] > 0)]
        parts.append(x)
    df = pd.concat(parts)
    df["spread"] = df["best_ask"] - df["best_bid"]
    df = df[df["spread"] < df["mark"] * 3 + 5]          # drop one-sided junk quotes
    df["bucket"] = pd.cut(df["mark"], EDGES, right=False, labels=False)

    rows, spread, p75, n = [], [], [], []
    for i in range(len(EDGES) - 1):
        g = df[df["bucket"] == i]["spread"]
        if len(g) < 50:
            med = spread[-1] if spread else 0.2
            q75 = p75[-1] if p75 else 0.3
        else:
            med, q75 = float(g.median()), float(g.quantile(0.75))
        spread.append(round(med, 3))
        p75.append(round(q75, 3))
        n.append(int(len(g)))
        mid = (EDGES[i] + min(EDGES[i + 1], EDGES[i] * 2 + 1)) / 2
        rows.append((f"{EDGES[i]:g}-{EDGES[i + 1]:g}" if EDGES[i + 1] < 1e8 else f"{EDGES[i]:g}+",
                     len(g), med, q75, 100 * med / max(mid, 0.5)))

    print(f"  {len(df):,} quotes from {len(files[::STEP])} archive chunks\n")
    print(f"  {'mark $':<12}{'quotes':>10}{'median spread':>15}{'p75':>9}{'~% of mark':>12}")
    for b, cnt, med, q75, pct in rows:
        print(f"  {b:<12}{cnt:>10,}{med:>15.2f}{q75:>9.2f}{pct:>11.1f}%")
    old = [max(2.0, 0.05 * m) for m in (0.5, 1.5, 3.5, 7.5, 15, 27, 42, 62, 87, 125, 175, 250, 400, 750)]
    print("\n  old model (max $2, 5%) at the same marks: " + ", ".join(f"{x:.2f}" for x in old))

    out = {"edges": EDGES[:-1], "spread": spread, "p75": p75, "n": n,
           "source": "data/chain_archive best_ask - best_bid, median per mark bucket",
           "quotes": int(len(df))}
    f = ROOT / "config" / "spread_model.json"
    f.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\n  written {f}")


if __name__ == "__main__":
    main()
