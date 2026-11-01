import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #1 — finishing runs: everything not covered by run_index.py.

    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.prabhu.run_finish [A] [B] [C]

A  NIFTY + BANK NIFTY: each strategy's in-sample-chosen config (from
   results/index_all_configs.csv) re-run with the book's filters
     F2 daily trend · F4 Nifty–Bank Nifty agreement · F2+F4 · F3 2% daily
     loss cap — plus the variants: 15-min opening range (1.6), pin-bar first
     candle (1.1), 20-EMA target (1.19 / 1.20)
B  182 NSE F&O stocks, 5-min (costs 0.05%/side), book defaults: 2R target,
   stop candle | level; config chosen in-sample, judged out-of-sample
C  BITCOIN 5-min 2017→2026 (UTC day = session), full option grid,
   tuning 2017–2022 / test 2023→
"""

from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.engine.universe import run_universe, summarize
from backtesting.book_strategies.prabhu.run_index import BOOK, NAMES, MOD, grid as index_grid

OUT = Path(__file__).resolve().parent / "results"
ORDER = {b[0]: i for i, b in enumerate(BOOK)}
pd.set_option("display.width", 320); pd.set_option("display.max_columns", 40)


def pick(summ, min_is=20):
    rows = []
    for sid, g in summ.groupby("strategy", sort=False):
        ok = g[g["IS_trades"] >= min_is]
        p = (ok if len(ok) else g).sort_values("IS_totalR_net", ascending=False).iloc[0].copy()
        p["cfgs_OOS_pos"] = f"{int((g['OOS_avgR_net'] > 0).sum())}/{len(g)}"
        rows.append(p)
    s = pd.DataFrame(rows)
    s.insert(1, "name", s["strategy"].map(NAMES))
    return s.assign(_o=s["strategy"].map(ORDER)).sort_values("_o").drop(columns="_o")


def loss_cap(t: pd.DataFrame, cap_r=2.0) -> pd.DataFrame:
    """F3: stop taking trades for the day once trades already CLOSED that day
    have lost cap_r (2% of capital = 2R). Applied per strategy/config across
    both indices together."""
    keep = []
    for _, g in t.groupby(["strategy", "cfg_id"]):
        g = g.sort_values("entry_time")
        booked = []                          # (exit_time, R)
        day = None
        for idx, r in g.iterrows():
            d = r.entry_time.normalize()
            if d != day:
                day, booked = d, []
            lost = sum(R for ex, R in booked if ex <= r.entry_time)
            if lost > -cap_r:
                keep.append(idx)
                booked.append((r.exit_time, r.R))
    return t.loc[keep]


def part_a():
    allc = pd.read_csv(OUT / "index_all_configs.csv")
    base = pick(allc)
    cfgs = {(sid, cid): cfg for sid, _, _, cid, cfg in index_grid()}
    fn_of = {b[0]: b[2] for b in BOOK}
    specs = []
    for _, r in base.iterrows():
        sid, cid = r["strategy"], r["cfg_id"]
        cfg = cfgs[(sid, cid)]
        for tag, extra in (("base", {}), ("F2 daily trend", {"dtrend": True}),
                           ("F4 Nifty-BN agree", {"agree": True}),
                           ("F2+F4", {"dtrend": True, "agree": True})):
            specs.append((sid, MOD, fn_of[sid], tag, {**cfg, **extra}))
        if sid == "1.6":
            specs.append((sid, MOD, "orb", "V 15-min range", {**cfg, "or_min": 15}))
        if sid == "1.1":
            specs.append((sid, MOD, "od", "V pin-bar first candle", {**cfg, "pin": True}))
        if sid in ("1.19", "1.20"):
            specs.append((sid, MOD, "gap_reject", "V 20-EMA target", {**cfg, "tgt": "level", "tgt_ema": True}))
    t = run_universe(specs, market="in_index_5m", workers=2)
    t["exit_time"] = pd.to_datetime(t["exit_time"]); t["entry_time"] = pd.to_datetime(t["entry_time"])
    capped = loss_cap(t[t.cfg_id == "base"]).assign(cfg_id="F3 2% daily loss cap")
    t = pd.concat([t, capped], ignore_index=True)
    s = summarize(t, "in_index_5m")
    s.insert(1, "name", s["strategy"].map(NAMES))
    s = s.assign(_o=s["strategy"].map(ORDER)).sort_values(["_o", "cfg_id"]).drop(columns="_o")
    s.to_csv(OUT / "finish_A_filters.csv", index=False)
    wide = s.pivot_table(index=["strategy"], columns="cfg_id", values="OOS_avgR_net", aggfunc="first")
    n = s.pivot_table(index=["strategy"], columns="cfg_id", values="OOS_trades", aggfunc="first")
    wide = wide.loc[sorted(wide.index, key=ORDER.get)]
    print("\nA) NIFTY+BN — test-period avg R after costs, by filter/variant (trades in brackets):")
    out = wide.copy().astype(object)
    for c in wide.columns:
        out[c] = [f"{v:+.2f} ({int(k)})" if v == v else "" for v, k in zip(wide[c], n[c].reindex(wide.index))]
    out.insert(0, "name", [NAMES[i] for i in out.index])
    print(out.to_string())


def part_b():
    specs = []
    for sid, _, fn, sides, grp in BOOK:
        for sl in ("candle", "level"):
            cfg = {"sl": sl, "tgt": "2R", "sides": sides, "harness": {}}
            if fn in ("od", "gap_reject"):
                cfg["entry"] = 1
            specs.append((sid, MOD, fn, f"sl={sl}", cfg))
    t = run_universe(specs, market="in_stocks_5m_lv")
    print(f"B) stocks: {len(t):,} trades")
    s = pick(summarize(t, "in_stocks_5m_lv"), min_is=100)
    s.to_csv(OUT / "finish_B_stocks.csv", index=False)
    s["OOS_Rs"] = (s["OOS_totalR_net"] * 10_000).round(0)
    print(s[["strategy", "name", "cfg_id", "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_gross",
             "OOS_avgR_net", "OOS_Rs", "cfgs_OOS_pos"]].to_string(index=False))


def part_c():
    specs = index_grid()
    t = run_universe(specs, market="btc_5m", workers=1)
    print(f"C) BTC: {len(t):,} trades")
    s = pick(summarize(t, "btc_5m"), min_is=50)
    s.to_csv(OUT / "finish_C_btc.csv", index=False)
    s["OOS_Rs"] = (s["OOS_totalR_net"] * 10_000).round(0)
    print(s[["strategy", "name", "cfg_id", "OOS_trades", "OOS_win_pct", "OOS_pf", "OOS_avgR_gross",
             "OOS_avgR_net", "OOS_Rs", "cfgs_OOS_pos"]].to_string(index=False))


if __name__ == "__main__":
    which = sys.argv[1:] or ["A", "B", "C"]
    for p in which:
        {"A": part_a, "B": part_b, "C": part_c}[p]()
