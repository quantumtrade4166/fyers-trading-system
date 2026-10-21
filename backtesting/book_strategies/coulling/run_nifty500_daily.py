import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Book #11 (Coulling VPA) — strategies 11.1–11.14 on Nifty 500, DAILY bars.

    .venv\\Scripts\\python.exe -m backtesting.book_strategies.coulling.run_nifty500_daily

Universe : Nifty 500 (current members — survivorship bias!), 2005 → 2026-09
Account  : Rs10 lakh, 1% risk per trade on CURRENT equity (compounds),
           cash only (no leverage), max 20% of capital in one stock,
           a trade that doesn't fit in free cash is skipped
Costs    : 0.15% per side (delivery STT + charges + slippage)
Sides    : LONG and SHORT run as separate accounts. Long = tradable in a cash
           account. Short = only possible via stock futures (~180 F&O names),
           shown for information.
Honesty  : config CHOSEN on in-sample (before 2019-01-01, long side, CAGR),
           REPORTED out-of-sample (2019-01-01 onward).
"""

import time
from pathlib import Path

import pandas as pd

from backtesting.engine.markets import MARKETS
from backtesting.engine.portfolio import simulate, stats
from backtesting.engine.universe import run_universe
from backtesting.book_strategies.coulling.run_in_stocks import NAMES, MOD

MARKET = "in_stocks_1d"
OUT = Path(__file__).resolve().parent / "results"
MIN_IS_TRADES = 100
D10 = {"max_bars": 10}        # default time cap: 10 trading days


def grid() -> list:
    g = []

    def add(sid, fn, cfgs):
        for k, c in enumerate(cfgs):
            for side in ("long", "short"):
                cc = {**c, "sides": side}
                cc.setdefault("harness", D10)
                label = ",".join(f"{a}={b}" for a, b in c.items() if a != "harness")
                hl = ",".join(f"{a}={b}" for a, b in cc["harness"].items())
                g.append((sid, MOD, fn, f"{k}|{label}|{hl}|{side}", cc))

    add("11.1", "s11_1", [{"rv_min": r} for r in (1.0, 1.5)])
    add("11.2", "s11_2", [{"body_min": b, "rv_max": v, "harness": {"max_bars": 5}}
                          for (b, v) in ((1.5, 0.75), (2.0, 0.5))])
    add("11.3", "s11_3", [{"confirm": c, "rv_min": r} for c in (True, False) for r in (1.75, 2.5)])
    add("11.4", "s11_4", [{"trend": t} for t in (1.0, 2.0)])
    add("11.5", "s11_5", [{"n_stop": n, "trend": 2.0} for n in (1, 2)])
    add("11.6", "s11_6", [{"rv_max": v} for v in (0.75, 1.0)])
    add("11.7", "s11_7", [{"rv_min": r, "min_bars": m, "max_age": 120, "harness": {"max_bars": 20}}
                          for r in (1.5, 2.0) for m in (10, 20)])
    add("11.8", "s11_8", [{"rv_max": v, "max_age": 120} for v in (1.0, 0.75)])
    add("11.9", "s11_9", [{"rv_min": r, "min_bars": 10, "max_age": 120,
                           "harness": {"trailing": True, "max_bars": 60}} for r in (1.5, 2.0)])
    add("11.10", "s11_10", [{"trend": t} for t in (1.0, 2.0)])
    add("11.11", "s11_11", [{"mode": m, "gap": gp, "harness": {"max_bars": 5}}
                            for m in ("follow", "fade") for gp in (0.02, 0.04)])
    add("11.12", "s11_12", [{"rv_pb": v} for v in (1.0, 0.75)])
    add("11.13", "s11_13", [{"pattern": p} for p in ("triangle", "triple")])
    add("11.14", "s11_14", [{"days": d} for d in (20, 60)])
    return g


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    mk = MARKETS[MARKET]
    specs = grid()
    print(f"{len(specs)} configs (long + short) x Nifty 500 …")
    t0 = time.time()
    trades = run_universe(specs, market=MARKET)
    print(f"signals done in {time.time() - t0:.0f}s — {len(trades):,} raw trades")
    trades.to_parquet(OUT / "nifty500_daily_trades.parquet", index=False)

    cut = mk["oos_start"]
    rows, curves = [], {}
    for (sid, cid), g in trades.groupby(["strategy", "cfg_id"]):
        acct, eq = simulate(g, mk["capital"])
        side = cid.rsplit("|", 1)[1]
        row = {"strategy": sid, "name": NAMES[sid], "side": side, "cfg_id": cid,
               "signals": len(g), "skipped_no_cash": int((~acct["taken"]).sum())}
        row.update({f"IS_{k}": v for k, v in stats(eq, acct, end=cut).items()})
        row.update({f"OOS_{k}": v for k, v in stats(eq, acct, start=cut).items()})
        row.update({f"ALL_{k}": v for k, v in stats(eq, acct).items()})
        rows.append(row)
        curves[cid + "#" + sid] = eq
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT / "nifty500_daily_all_configs.csv", index=False)
    pd.DataFrame(curves).to_parquet(OUT / "nifty500_daily_equity.parquet")

    # choose each strategy's config on the LONG side, in-sample CAGR only
    pick = []
    for sid, g in summ[summ["side"] == "long"].groupby("strategy", sort=False):
        ok = g[g["IS_trades"] >= MIN_IS_TRADES]
        best = (ok if len(ok) else g).sort_values("IS_cagr_pct", ascending=False).iloc[0]
        short = summ[(summ["strategy"] == sid) & (summ["side"] == "short")
                     & (summ["cfg_id"] == best["cfg_id"].rsplit("|", 1)[0] + "|short")]
        pick.append(best)
        if len(short):
            pick.append(short.iloc[0])
    sel = pd.DataFrame(pick)
    sel["_k"] = sel["strategy"].str.split(".").str[1].astype(int)
    sel = sel.sort_values(["_k", "side"]).drop(columns="_k")
    sel.to_csv(OUT / "nifty500_daily_selected.csv", index=False)

    cols = ["strategy", "name", "side", "IS_trades", "IS_cagr_pct", "IS_maxdd_pct",
            "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_gross", "OOS_avgR_net",
            "OOS_cagr_pct", "OOS_maxdd_pct", "OOS_sharpe", "skipped_no_cash"]
    pd.set_option("display.width", 260); pd.set_option("display.max_columns", 30)
    print("\nLong config chosen in-sample (short shown with the same settings), judged out-of-sample:")
    print(sel[cols].to_string(index=False))
