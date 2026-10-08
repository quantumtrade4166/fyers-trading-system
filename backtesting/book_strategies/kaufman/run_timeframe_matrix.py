import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Full timeframe matrix — every tested strategy × every timeframe
==================================================================
Both batches used inconsistent, incomplete timeframe sets (Batch C: 1D/4h/1h/15min,
Batch B: 1D/4h/1h). Both peaked at the COARSEST timeframe tested, which means the
optimum may lie above 1D where we never looked. This runs the full ladder from
15min to weekly.

⚠️ Multiple-testing warning: 15 strategies × 9 timeframes = 135 combinations.
Some will look good by chance alone. Treat the winner as a hypothesis to
validate out-of-sample, not as a discovery.

Run: python backtesting/book_strategies/kaufman/run_timeframe_matrix.py
"""

import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, TEST_YEARS, START_DATE, END_DATE
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as ADAPTIVE
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as CHANNELS, STRATEGY_CFG as CHANNEL_CFG,
)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000
RETAIL_COST = 0.35

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_per_unit": RETAIL_COST,
}

# Full ladder — coarse end extended well past where we previously stopped
TIMEFRAMES = ["15min", "30min", "1h", "2h", "4h", "8h", "12h", "1D", "1W"]

PER_STRATEGY = {
    **CHANNEL_CFG,
    "S092_AdaptiveBrkout": {"trailing": True},
}

REGISTRY = {**ADAPTIVE, **CHANNELS}


def main():
    t0 = time.perf_counter()
    print("=" * 120)
    print("  FULL TIMEFRAME MATRIX — all Kaufman strategies tested so far")
    print(f"  XAUUSD  {START_DATE} -> {END_DATE} ({TEST_YEARS}y)  |  ${CAPITAL:,} start, 1% risk, "
          f"cost ${RETAIL_COST:.2f}/oz")
    print(f"  {len(REGISTRY)} strategies x {len(TIMEFRAMES)} timeframes = {len(REGISTRY)*len(TIMEFRAMES)} runs")
    print("=" * 120)

    rows = []
    for tf in TIMEFRAMES:
        try:
            df = get_bars(tf)
        except Exception as exc:
            print(f"  [{tf}] SKIPPED: {exc}")
            continue
        print(f"\n  [{tf}] {len(df):,} bars")

        for name, fn in REGISTRY.items():
            cfg = {**BASE_CFG, **PER_STRATEGY.get(name, {})}
            try:
                _, m = backtest(df.copy(), fn, cfg)
            except Exception as exc:
                continue
            if m.get("total_trades", 0) == 0:
                continue
            rows.append({"strategy": name, "timeframe": tf, **m})

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_timeframe_matrix_{END_DATE}.csv"
    res.to_csv(out, index=False)

    # ── Net $ pivot: strategy × timeframe ────────────────────────────────
    print("\n" + "=" * 120)
    print("  NET PROFIT (USD) — strategy x timeframe   [$20,000 start, after cost]")
    print("=" * 120)
    piv = res.pivot_table(index="strategy", columns="timeframe",
                          values="net_pnl_usd", aggfunc="first")
    piv = piv.reindex(columns=[t for t in TIMEFRAMES if t in piv.columns])
    print(piv.round(0).fillna(0).astype(int).to_string())

    print("\n" + "=" * 120)
    print("  SHARPE — strategy x timeframe")
    print("=" * 120)
    piv_s = res.pivot_table(index="strategy", columns="timeframe",
                            values="sharpe", aggfunc="first")
    piv_s = piv_s.reindex(columns=[t for t in TIMEFRAMES if t in piv_s.columns])
    print(piv_s.round(2).to_string())

    # ── Which timeframe is best overall? ─────────────────────────────────
    print("\n" + "=" * 120)
    print("  TIMEFRAME SUMMARY — how each timeframe performs across all strategies")
    print("=" * 120)
    agg = res.groupby("timeframe").agg(
        strategies=("strategy", "count"),
        profitable=("net_pnl_usd", lambda s: int((s > 0).sum())),
        median_net=("net_pnl_usd", "median"),
        best_net=("net_pnl_usd", "max"),
        median_sharpe=("sharpe", "median"),
        best_sharpe=("sharpe", "max"),
    ).reindex([t for t in TIMEFRAMES if t in res["timeframe"].unique()])
    print(agg.round(2).to_string())

    print("\n" + "=" * 120)
    print("  TOP 20 COMBINATIONS BY NET PROFIT")
    print("=" * 120)
    cols = ["strategy", "timeframe", "total_trades", "win_rate_pct", "profit_factor",
            "avg_rr", "sharpe", "cagr_pct", "max_drawdown_pct", "net_pnl_usd", "final_equity_usd"]
    print(res.sort_values("net_pnl_usd", ascending=False)[cols].head(20).to_string(index=False))

    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
