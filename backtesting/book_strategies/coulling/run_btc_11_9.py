import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Book #11 strategy 11.9 (volume-confirmed congestion breakout + dynamic
pivot trail) on Bitcoin — Binance BTCUSDT, real traded volume.

    .venv\\Scripts\\python.exe -m backtesting.book_strategies.coulling.run_btc_11_9

Timeframes : 1D, 4h, 1h (resampled from the 1-min archive, 2017-08 → 2026-09)
Account    : Rs10 lakh, 1% risk per trade on current equity (compounds),
             no leverage (position <= equity)
Costs      : 0.06% per side (Delta/Binance perp taker + slippage) and 10%/yr
             funding paid by longs, received by shorts (same as Kaufman BTC)
Sides      : long, short, and both together (a perp can do either)
Honesty    : config chosen on in-sample 2017-08 → 2021-12 (CAGR),
             reported out-of-sample 2022-01 → 2026-09
"""

from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.engine import harness, vpa
from backtesting.engine.portfolio import simulate, stats
from backtesting.engine.universe import combine_sides
from backtesting.book_strategies.coulling.strategies import s11_9
from backtesting.book_strategies.kaufman.data_btc import get_bars

OUT = Path(__file__).resolve().parent / "results"
CAPITAL, OOS = 1_000_000.0, "2022-01-01"
H = {"capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 1.0,
     "entry_on": "next_open", "cost_pct": 0.0006, "funding_apr": 0.10,
     "calendar": "calendar", "atr_col": "atr", "trailing": True}


def bench(close: pd.Series, a=None, b=None) -> tuple:
    x = close
    if a: x = x[x.index >= a]
    if b: x = x[x.index < b]
    x = x.resample("D").last().dropna()
    yrs = (x.index[-1] - x.index[0]).days / 365.25
    return (round(((x.iloc[-1] / x.iloc[0]) ** (1 / yrs) - 1) * 100, 1),
            round(((x / x.cummax()) - 1).min() * 100, 1))


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    rows = []
    grid = [{"rv_min": r, "min_bars": m, "max_h": h, "max_age": 120}
            for r in (1.5, 2.0) for m in (10, 20) for h in (3.0, 5.0)]
    for tf in ("1D", "4h", "1h"):
        df = get_bars(tf)
        f = vpa.add_vpa_features(df, intraday=False)
        fm = vpa.add_vpa_features(vpa.mirror(df), intraday=False)
        for k, cfg in enumerate(grid):
            for side in ("long", "short", "both"):
                sig = combine_sides(s11_9, f, fm, {**cfg, "sides": side})
                tr = harness.run(f, sig, H)
                if not tr:
                    continue
                t = pd.DataFrame([x.to_dict() for x in tr])
                t["R"] = t["pnl"] / (CAPITAL * 0.01); t["R_gross"] = t["gross_pnl"] / (CAPITAL * 0.01)
                acct, eq = simulate(t, CAPITAL, calendar="calendar")
                row = {"tf": tf, "cfg": k, "side": side,
                       **{a: cfg[a] for a in ("rv_min", "min_bars", "max_h")}}
                row.update({f"IS_{a}": v for a, v in stats(eq, acct, end=OOS, calendar="calendar").items()})
                row.update({f"OOS_{a}": v for a, v in stats(eq, acct, start=OOS, calendar="calendar").items()})
                rows.append(row)
        print(f"{tf} done", flush=True)

    allr = pd.DataFrame(rows)
    allr.to_csv(OUT / "btc_11_9_all.csv", index=False)

    # pick per timeframe + side on IN-SAMPLE CAGR (min 20 trades)
    pick = []
    for (tf, side), g in allr.groupby(["tf", "side"], sort=False):
        ok = g[g["IS_trades"] >= 20]
        pick.append((ok if len(ok) else g).sort_values("IS_cagr_pct", ascending=False).iloc[0])
    sel = pd.DataFrame(pick)
    sel.to_csv(OUT / "btc_11_9_selected.csv", index=False)

    close = get_bars("1D")["close"]
    print("\nBTC buy & hold:  in-sample CAGR/DD", bench(close, b=OOS),
          "| out-of-sample CAGR/DD", bench(close, a=OOS))
    cols = ["tf", "side", "rv_min", "min_bars", "max_h", "IS_trades", "IS_cagr_pct", "IS_maxdd_pct",
            "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_gross", "OOS_avgR_net",
            "OOS_cagr_pct", "OOS_maxdd_pct", "OOS_sharpe"]
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(sel[cols].to_string(index=False))
    print("\nOOS spread across ALL 8 configs (median CAGR) — robustness check:")
    print(allr.groupby(["tf", "side"])["OOS_cagr_pct"].agg(["median", "min", "max"]).round(1).to_string())
