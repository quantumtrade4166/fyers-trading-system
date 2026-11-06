import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Orderflow strategy screen on Binance BTC/ETH perps.

For every perp x timeframe x strategy x params x exit:
  simulate the whole history once (gross), then split trades by entry date:
    IN-SAMPLE  2020-01-01 .. 2023-12-31   (choose params here)
    OUT-SAMPLE 2024-01-01 .. end          (never used for choosing)
Costs: taker 7 bps/side (Delta 0.05% + 18% GST = 5.9 bps, + ~1 bp slippage),
       also a 3 bps/side "maker" view.

Run: .venv\\Scripts\\python.exe -m backtesting.orderflow.run_screen
Out: backtesting/orderflow/results/screen_all.csv, screen_best.csv
"""
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.orderflow.bars import make_bars
from backtesting.orderflow.engine import simulate, stats
from backtesting.orderflow.strategies import EXITS, STRATEGIES

OUT = Path(__file__).parent / "results"
SPLIT = pd.Timestamp("2024-01-01")
COSTS = {"taker": 0.0007, "maker": 0.0003}
PERPS = ["BTCUSDT", "ETHUSDT"]
TFS = ["5min", "15min", "1h"]


def main() -> None:
    OUT.mkdir(exist_ok=True)
    rows = []
    t0 = time.time()
    for perp in PERPS:
        for tf in TFS:
            b = make_bars(perp, tf)
            idx = b.index
            o, h, l, c, atr = (b[x].to_numpy() for x in ("open", "high", "low", "close", "atr"))
            is_mask_end = idx.searchsorted(SPLIT)
            for sname, (fn, grid) in STRATEGIES.items():
                for p in grid:
                    sig = fn(b, **p)
                    for ex in EXITS:
                        ent, exi, gross = simulate(o, h, l, c, atr, sig, ex["hold"],
                                                   ex["stop_k"], ex["tgt_k"], 0.0)
                        ins = ent < is_mask_end
                        row = dict(perp=perp, tf=tf, strategy=sname,
                                   params=str(p), **ex)
                        for part, m, sub in (("is", ins, idx[:is_mask_end]),
                                             ("oos", ~ins, idx[is_mask_end:])):
                            row[f"{part}_gross_bps"] = gross[m].mean() * 1e4 if m.any() else np.nan
                            for cname, cost in COSTS.items():
                                st = stats(idx, ent[m], exi[m], gross[m] - 2 * cost)
                                for kk in ("trades", "win", "avg_bps", "total", "pf", "sharpe", "maxdd"):
                                    if cname == "maker" and kk in ("trades", "win"):
                                        continue
                                    row[f"{part}_{cname}_{kk}"] = st[kk]
                        rows.append(row)
            print(f"{perp} {tf}: {len(b):,} bars, {len(rows)} runs so far "
                  f"({time.time() - t0:.0f}s)", flush=True)
    df = pd.DataFrame(rows)
    df.to_csv(OUT / "screen_all.csv", index=False)

    # best in-sample per perp x tf x strategy (taker Sharpe, >= 100 IS trades)
    ok = df[df["is_taker_trades"] >= 100]
    best = ok.loc[ok.groupby(["perp", "tf", "strategy"])["is_taker_sharpe"].idxmax()]
    best.to_csv(OUT / "screen_best.csv", index=False)

    # robustness: share of ALL combos that are positive out-of-sample after taker cost
    rob = df.groupby(["strategy"]).agg(
        combos=("oos_taker_total", "size"),
        oos_pos_taker=("oos_taker_total", lambda s: (s > 0).mean() * 100),
        oos_pos_maker=("oos_maker_total", lambda s: (s > 0).mean() * 100),
        med_oos_gross_bps=("oos_gross_bps", "median"))
    rob.to_csv(OUT / "screen_robustness.csv")
    pd.set_option("display.width", 250)
    cols = ["perp", "tf", "strategy", "params", "hold", "stop_k", "tgt_k",
            "is_taker_trades", "is_gross_bps", "is_taker_sharpe",
            "oos_taker_trades", "oos_gross_bps", "oos_taker_avg_bps", "oos_taker_total",
            "oos_taker_sharpe", "oos_taker_maxdd", "oos_maker_total", "oos_maker_sharpe"]
    print(best[cols].round(2).to_string(index=False))
    print(rob.round(1).to_string())
    print(f"done {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
