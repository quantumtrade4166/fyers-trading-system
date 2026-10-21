import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Book #11 (Coulling VPA) — strategies 11.1–11.14 on Indian stocks, 5-minute.

    .venv\\Scripts\\python.exe -m backtesting.book_strategies.coulling.run_in_stocks

Universe : every NSE F&O stock with 5-min data (182), 2024-05-28 → 2026-06-25
Session  : entries 09:20–14:55, everything squared off at the 15:15 bar (MIS)
Sizing   : fixed risk Rs1,000 per trade (= 1R), notional capped at Rs5L
Costs    : 0.05% per side on notional (charges + slippage); gross also kept
Honesty  : each strategy's config is CHOSEN on in-sample (exits before
           2025-10-01) and REPORTED on out-of-sample (2025-10-01 onward).

Outputs (results/):
    in_stocks_trades.parquet        every trade, every config
    in_stocks_all_configs.csv       IS + OOS stats for all configs
    in_stocks_selected.csv          one row per strategy: IS-chosen config + its OOS
"""

import time
from pathlib import Path

import pandas as pd

from backtesting.engine.universe import run_universe, summarize

MOD = "backtesting.book_strategies.coulling.strategies"
OUT = Path(__file__).resolve().parent / "results"
MIN_IS_TRADES = 150
T12 = {"max_bars": 12}       # default time cap: 12 bars = 1 hour

NAMES = {
    "11.1": "Validated Trend Join", "11.2": "No-Effort Trap Fade",
    "11.3": "Absorption Reversal", "11.4": "Double Hammer / Shooting Star",
    "11.5": "Stopping / Topping-Out Volume", "11.6": "Low-Volume Test",
    "11.7": "Volume Congestion Breakout", "11.8": "Low-Volume Fake-Out Fade",
    "11.9": "Breakout + Dynamic Pivot Trail", "11.10": "Hanging Man + Star",
    "11.11": "Gap Validation", "11.12": "Pullback on Falling Volume",
    "11.13": "Triangle / Triple Top-Bottom", "11.14": "Volume-at-Price Breakout",
}


def grid() -> list:
    g = []

    def add(sid, fn, cfgs):
        for k, c in enumerate(cfgs):
            c = {**c}
            c.setdefault("harness", T12)
            label = ",".join(f"{a}={b}" for a, b in c.items() if a != "harness")
            hl = ",".join(f"{a}={b}" for a, b in c["harness"].items())
            g.append((sid, MOD, fn, f"{k}|{label}|{hl}", c))

    add("11.1", "s11_1", [{"rv_min": r, "harness": {"max_bars": m}} for r in (1.0, 1.5) for m in (6, 12)])
    add("11.2", "s11_2", [{"body_min": b, "rv_max": v, "open_only": o, "harness": {"max_bars": 6}}
                          for (b, v) in ((1.5, 0.75), (2.0, 0.5)) for o in (False, True)])
    add("11.3", "s11_3", [{"confirm": c, "rv_min": r} for c in (True, False) for r in (1.75, 2.5)])
    add("11.4", "s11_4", [{"trend": t} for t in (1.0, 2.0)])
    add("11.5", "s11_5", [{"n_stop": n, "trend": 2.0} for n in (1, 2)])
    add("11.6", "s11_6", [{"rv_max": v} for v in (0.75, 1.0)])
    add("11.7", "s11_7", [{"rv_min": r, "min_bars": m, "harness": {"max_bars": 24}}
                          for r in (1.5, 2.0) for m in (12, 24)])
    add("11.8", "s11_8", [{"rv_max": v} for v in (1.0, 0.75)])
    add("11.9", "s11_9", [{"rv_min": r, "min_bars": 12, "harness": {"trailing": True}} for r in (1.5, 2.0)])
    add("11.10", "s11_10", [{"trend": t} for t in (1.0, 2.0)])
    add("11.11", "s11_11", [{"mode": m, "gap": gp, "harness": {}}
                            for m in ("follow", "fade") for gp in (0.005, 0.01)])
    add("11.12", "s11_12", [{"rv_pb": v} for v in (1.0, 0.75)])
    add("11.13", "s11_13", [{"pattern": p} for p in ("triangle", "triple")])
    add("11.14", "s11_14", [{"days": d} for d in (5, 10)])
    return g


def select(summary: pd.DataFrame) -> pd.DataFrame:
    """Pick each strategy's config on IN-SAMPLE net R only."""
    rows = []
    for sid, g in summary.groupby("strategy", sort=False):
        ok = g[g["IS_trades"] >= MIN_IS_TRADES]
        pick = (ok if len(ok) else g).sort_values("IS_totalR_net", ascending=False).iloc[0]
        rows.append(pick)
    sel = pd.DataFrame(rows)
    sel.insert(1, "name", sel["strategy"].map(NAMES))
    key = sel["strategy"].str.split(".").str[1].astype(int)
    return sel.assign(_k=key).sort_values("_k").drop(columns="_k")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    specs = grid()
    print(f"{len(specs)} configs x all stocks …")
    t0 = time.time()
    trades = run_universe(specs, market="in_stocks_5m")
    print(f"done in {time.time() - t0:.0f}s — {len(trades):,} trades")
    trades.to_parquet(OUT / "in_stocks_trades.parquet", index=False)

    summ = summarize(trades, "in_stocks_5m")
    summ.to_csv(OUT / "in_stocks_all_configs.csv", index=False)
    sel = select(summ)
    sel.to_csv(OUT / "in_stocks_selected.csv", index=False)

    cols = ["strategy", "name", "IS_trades", "IS_pf", "IS_avgR_net", "IS_totalR_net",
            "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_gross", "OOS_avgR_net",
            "OOS_totalR_net", "OOS_sharpe", "OOS_maxdd_R"]
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print("\nIS-chosen config per strategy, judged out-of-sample:")
    print(sel[cols].to_string(index=False))
