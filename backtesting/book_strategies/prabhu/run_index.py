import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #1 (Prabhu, 25 Day Trading Strategies) — all 25 strategies on NIFTY and
BANK NIFTY, 5-minute, traded as index futures.

    .venv\Scripts\python.exe -m backtesting.book_strategies.prabhu.run_index

Data    : real index 5-min bars 2024-05-28 → 2026-06-25 (only ~2 years on disk)
Session : entries 09:15-close → 14:55, square-off 15:15 bar (15:20)
Risk    : fixed Rs10,000 (1% of Rs10L) = 1R per trade; futures costs 0.02%/side
Exits   : stop, 2R target (or the book's level), session end
Configs : the book's own choices — stop (candle | level) x target (2R | level)
          x CPR day-type filter (off | on) [+ entry 1/2, EMA 8/20 where the
          book gives both]
Honesty : each strategy's config CHOSEN on in-sample (exits before
          2025-09-01) and REPORTED out-of-sample (2025-09-01 →).
"""

import time
from pathlib import Path

import pandas as pd

from backtesting.engine.universe import run_universe, summarize

MOD = "backtesting.book_strategies.prabhu.strategies"
OUT = Path(__file__).resolve().parent / "results"
MARKET = "in_index_5m"
MIN_IS_TRADES = 20

# (ID, name, function, sides, day-type group)  — group: T trending, S sideways, R reversal
BOOK = [
    ("1.1", "Open Drive", "od", "both", "T"),
    ("1.2", "Morning Star", "star_reclaim", "long", "T"),
    ("1.3", "Evening Star", "star_reclaim", "short", "T"),
    ("1.4", "CPR Breakout", "cprbo", "both", "T"),
    ("1.5", "Supply Zone Breakout", "zone_break", "long", "T"),
    ("1.6", "Opening Range Breakout", "orb", "both", "T"),
    ("1.7", "Day High Breakout", "dayhigh_break", "long", "T"),
    ("1.8", "Gap Filling", "gap_fill", "both", "T"),
    ("1.9", "MA Trend Following", "ma_trend", "both", "T"),
    ("1.10", "PPT", "ppt", "both", "S"),
    ("1.11", "Green Candle Retracement", "retrace", "long", "S"),
    ("1.12", "Red Candle Retracement", "retrace", "short", "S"),
    ("1.13", "M Pattern", "double_test", "short", "S"),
    ("1.14", "W Pattern", "double_test", "long", "S"),
    ("1.15", "Open Drive Rejection", "odr", "both", "R"),
    ("1.16", "Green Candle Breakout", "fc_break", "short", "R"),
    ("1.17", "Red Candle Breakout", "fc_break", "long", "R"),
    ("1.18", "Virgin CPR Reversal", "vcpr_rev", "both", "R"),
    ("1.19", "Gap Down Rejection", "gap_reject", "long", "R"),
    ("1.20", "Gap Up Rejection", "gap_reject", "short", "R"),
    ("1.21", "CPR Reversal", "cpr_rev", "both", "R"),
    ("1.22", "Extreme Candle Reversal", "extreme_rev", "both", "R"),
    ("1.23", "Supply Zone Reversal", "zone_rev", "short", "R"),
    ("1.24", "Gap Border Rejection", "gapborder_rej", "short", "R"),
    ("1.25", "Fake Breakout", "fake_break", "short", "R"),
    # mirror twins of one-sided book drawings (not book IDs)
    ("1.5x", "Demand Zone Breakdown (mirror)", "zone_break", "short", "T"),
    ("1.7x", "Day Low Breakdown (mirror)", "dayhigh_break", "short", "T"),
    ("1.23x", "Demand Zone Reversal (mirror)", "zone_rev", "long", "R"),
    ("1.24x", "Gap Border Bounce (mirror)", "gapborder_rej", "long", "R"),
    ("1.25x", "Fake Breakdown (mirror)", "fake_break", "long", "R"),
]
NAMES = {b[0]: b[1] for b in BOOK}


def grid() -> list:
    specs = []
    for sid, _, fn, sides, grp in BOOK:
        extra = [{}]
        if fn in ("od", "gap_reject"):
            extra = [{"entry": 1}, {"entry": 2}]
        elif fn == "ma_trend":
            extra = [{"ema": 8}, {"ema": 20}]
        dts = [None] if grp == "R" else [None, "trend" if grp == "T" else "side"]
        k = 0
        for e in extra:
            for sl in ("candle", "level"):
                for tg in ("2R", "level"):
                    for dt in dts:
                        cfg = {**e, "sl": sl, "tgt": tg, "daytype": dt, "sides": sides, "harness": {}}
                        lab = ",".join(f"{a}={b}" for a, b in cfg.items() if a not in ("harness", "sides"))
                        specs.append((sid, MOD, fn, f"{k}|{lab}", cfg))
                        k += 1
    return specs


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    specs = grid()
    print(f"{len(specs)} configs over {len(BOOK)} strategy IDs x 2 indices …")
    t0 = time.time()
    trades = run_universe(specs, market=MARKET, workers=2)
    print(f"done in {time.time()-t0:.0f}s — {len(trades):,} trades")
    trades.to_parquet(OUT / "index_trades.parquet", index=False)

    summ = summarize(trades, MARKET)
    summ.to_csv(OUT / "index_all_configs.csv", index=False)
    rows = []
    for sid, g in summ.groupby("strategy", sort=False):
        ok = g[g["IS_trades"] >= MIN_IS_TRADES]
        pick = (ok if len(ok) else g).sort_values("IS_totalR_net", ascending=False).iloc[0].copy()
        pick["OOS_median_avgR_all_configs"] = round(g["OOS_avgR_net"].median(), 3)
        pick["configs_OOS_positive"] = f"{int((g['OOS_avgR_net'] > 0).sum())}/{len(g)}"
        rows.append(pick)
    sel = pd.DataFrame(rows)
    sel.insert(1, "name", sel["strategy"].map(NAMES))
    order = {b[0]: i for i, b in enumerate(BOOK)}
    sel = sel.assign(_o=sel["strategy"].map(order)).sort_values("_o").drop(columns="_o")
    sel["OOS_Rs"] = (sel["OOS_totalR_net"] * 10_000).round(0)
    sel.to_csv(OUT / "index_selected.csv", index=False)

    cols = ["strategy", "name", "IS_trades", "IS_avgR_net", "IS_totalR_net",
            "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_gross", "OOS_avgR_net",
            "OOS_totalR_net", "OOS_Rs", "OOS_maxdd_R", "OOS_median_avgR_all_configs", "configs_OOS_positive", "cfg_id"]
    pd.set_option("display.width", 320); pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 60)
    print("\nIS-chosen config per strategy, judged out-of-sample (Sep-2025 → Jun-2026):")
    print(sel[cols].to_string(index=False))
