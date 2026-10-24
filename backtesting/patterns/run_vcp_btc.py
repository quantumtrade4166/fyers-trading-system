import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
VCP (Volatility Contraction Pattern) on Bitcoin — Binance BTCUSDT, real volume.

    .venv\Scripts\python.exe -m backtesting.patterns.run_vcp_btc

Timeframes : 1D, 4h, 1h (moving averages / 52-week high scaled to the
             same number of DAYS on every timeframe)
Account    : $10,000, 1% risk per trade on current equity, no leverage
Costs      : 0.06% per side + 10%/yr funding (longs pay)
Sides      : long (the pattern as designed) and both (mirror short added)
Honesty    : config chosen on 2017-08 → 2021-12 CAGR, reported on 2022 → 2026-09
"""

from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.engine import harness, vpa
from backtesting.engine.portfolio import simulate, stats
from backtesting.engine.universe import combine_sides
from backtesting.patterns.vcp import vcp
from backtesting.book_strategies.kaufman.data_btc import get_bars

OUT = Path(__file__).resolve().parent / "results"
CAP, OOS = 10_000.0, "2022-01-01"
H = {"capital": CAP, "risk_pct": 0.01, "max_position_pct": 1.0, "entry_on": "next_open",
     "cost_pct": 0.0006, "funding_apr": 0.10, "calendar": "calendar", "atr_col": "atr"}
PER_DAY = {"1D": 1, "4h": 6, "1h": 24}


def lookahead_ok(f, fm, cfg) -> bool:
    cut = int(len(f) * 0.8)
    a = combine_sides(vcp, f, fm, cfg).iloc[:cut]
    b = combine_sides(vcp, f.iloc[:cut], fm.iloc[:cut], cfg)
    return bool((a["entry"].to_numpy() == b["entry"].to_numpy()).all()
                and np.allclose(a["stop_long"], b["stop_long"], equal_nan=True))


def run_one(f, fm, cfg):
    tr = harness.run(f, combine_sides(vcp, f, fm, cfg), H)
    if not tr:
        return None, None
    t = pd.DataFrame([x.to_dict() for x in tr])
    t["R"] = t["pnl"] / (CAP * 0.01); t["R_gross"] = t["gross_pnl"] / (CAP * 0.01)
    return simulate(t, CAP, calendar="calendar")


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    rows, best = [], {}
    for tf, k in PER_DAY.items():
        df = get_bars(tf)
        f, fm = vpa.add_vpa_features(df, False), vpa.add_vpa_features(vpa.mirror(df), False)
        scale = {"ma_fast": 50 * k, "ma_slow": 200 * k, "yr": 365 * k, "max_wait": 20 * k}
        grid = [{**scale, "n_c": nc, "z": z, "dry": dr, "trail": tr * k if tf == "1D" else tr * k}
                for nc in (2, 3) for z in (1.5, 2.5) for dr in (0.8, 0) for tr in (20, 50)]
        print(f"{tf}: lookahead check {'PASS' if lookahead_ok(f, fm, grid[0]) else 'FAIL'}", flush=True)
        for i, cfg in enumerate(grid):
            for side in ("long", "both"):
                acct, eq = run_one(f, fm, {**cfg, "sides": side})
                if acct is None:
                    continue
                row = {"tf": tf, "cfg": i, "side": side, "n_c": cfg["n_c"], "z": cfg["z"],
                       "dry": cfg["dry"], "trail_days": cfg["trail"] // k}
                row.update({f"IS_{a}": v for a, v in stats(eq, acct, end=OOS, calendar="calendar").items()})
                row.update({f"OOS_{a}": v for a, v in stats(eq, acct, start=OOS, calendar="calendar").items()})
                row.update({f"ALL_{a}": v for a, v in stats(eq, acct, calendar="calendar").items()})
                rows.append(row)
        print(f"{tf} done", flush=True)
    allr = pd.DataFrame(rows)
    allr.to_csv(OUT / "vcp_btc_all.csv", index=False)

    pick = []
    for (tf, side), g in allr.groupby(["tf", "side"], sort=False):
        ok = g[g["IS_trades"] >= 10]
        pick.append((ok if len(ok) else g).sort_values("IS_cagr_pct", ascending=False).iloc[0])
    sel = pd.DataFrame(pick)
    sel.to_csv(OUT / "vcp_btc_selected.csv", index=False)
    pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)
    cols = ["tf", "side", "n_c", "z", "dry", "trail_days", "IS_trades", "IS_cagr_pct", "IS_maxdd_pct",
            "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_net", "OOS_cagr_pct", "OOS_maxdd_pct",
            "OOS_sharpe", "ALL_trades", "ALL_cagr_pct", "ALL_end_equity"]
    print(sel[cols].to_string(index=False))
    print("\nOOS CAGR across ALL 16 configs per timeframe/side (robustness):")
    print(allr.groupby(["tf", "side"])["OOS_cagr_pct"].agg(["median", "min", "max"]).round(1).to_string())
    print("\nTrades per config (median, whole period):")
    print(allr.groupby(["tf", "side"])["ALL_trades"].median().to_string())
