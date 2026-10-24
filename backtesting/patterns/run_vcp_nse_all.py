import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
VCP — SURVIVORSHIP-FREE test on every NSE stock (bhavcopy archive), daily.

    .venv\Scripts\python.exe -m backtesting.patterns.run_vcp_nse_all

Universe : all NSE stocks 2005 → 2026-09 (incl. later delisted/crashed);
           a stock may be BOUGHT only while it is in that day's top 500 by
           63-day traded value (point-in-time Nifty-500 stand-in)
Account  : Rs10 lakh, 1% risk, cash only, max 20%/stock, 0.15%/side
Versions : B = no size limit ; C = position <= 5% of the stock's 20-day
           average daily traded value (what a growing account can really fill)
Data     : trades spanning an overnight jump beyond -40%/+67% (likely an
           unadjusted corporate action) are EXCLUDED and counted
Honesty  : each version's config chosen on 2005–2018 CAGR, reported 2019 →
"""

import time
from pathlib import Path

import pandas as pd

from backtesting.engine.markets import MARKETS
from backtesting.engine.portfolio import simulate, stats
from backtesting.engine.universe import run_universe
from backtesting.patterns.run_vcp_nifty500 import grid

MARKET, OUT = "nse_all_1d", Path(__file__).resolve().parent / "results"
PREV = "11|n_c=3,z=1.5,dry=0,trail=50"     # the config chosen in the biased run


def yearly(acct, eq, cap):
    a = acct[acct.taken].copy(); a["year"] = a["exit_time"].dt.year
    rows = []
    for y, g in a.groupby("year"):
        e = eq[eq.index.year == y]
        st = eq[eq.index < e.index[0]].iloc[-1] if (eq.index < e.index[0]).any() else cap
        rows.append({"year": y, "trades": len(g), "start_Rs": round(st), "pnl_Rs": round(g.pnl_acct.sum()),
                     "end_Rs": round(e.iloc[-1]), "return_%": round((e.iloc[-1] / st - 1) * 100, 1)})
    return pd.DataFrame(rows)


if __name__ == "__main__":
    mk = MARKETS[MARKET]
    t0 = time.time()
    trades = run_universe(grid(), market=MARKET)
    print(f"signals done in {time.time()-t0:.0f}s — {len(trades):,} raw trades, "
          f"{trades.symbol.nunique():,} symbols traded")
    trades.to_parquet(OUT / "vcp_nse_all_trades.parquet", index=False)
    nj = int(trades.data_jump.sum())
    print(f"excluded {nj:,} trades that span a likely unadjusted corporate action "
          f"({nj/len(trades)*100:.1f}%)")
    clean = trades[~trades.data_jump]

    rows, keep = [], {}
    for ver, frac in (("B_no_cap", None), ("C_cap5pctADV", 0.05)):
        for cid, g in clean.groupby("cfg_id"):
            acct, eq = simulate(g, mk["capital"], adv_frac=frac)
            row = {"version": ver, "cfg_id": cid}
            for tag, kw in (("IS", {"end": mk["oos_start"]}), ("OOS", {"start": mk["oos_start"]}), ("ALL", {})):
                row.update({f"{tag}_{a}": v for a, v in stats(eq, acct, **kw).items()})
            a = acct[acct.taken]
            row["top10_share_%"] = round(a.pnl_acct.nlargest(10).sum() / a.pnl_acct.sum() * 100) if a.pnl_acct.sum() > 0 else None
            rows.append(row); keep[(ver, cid)] = (acct, eq)
    s = pd.DataFrame(rows)
    s.to_csv(OUT / "vcp_nse_all_summary.csv", index=False)

    pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)
    cols = ["cfg_id", "IS_trades", "IS_cagr_pct", "IS_maxdd_pct", "OOS_trades", "OOS_pf", "OOS_avgR_net",
            "OOS_cagr_pct", "OOS_maxdd_pct", "OOS_sharpe", "ALL_cagr_pct", "ALL_end_equity", "top10_share_%"]
    for ver in ("B_no_cap", "C_cap5pctADV"):
        v = s[s.version == ver].sort_values("IS_cagr_pct", ascending=False)
        print(f"\n===== {ver} — all 16 configs (sorted by in-sample CAGR) =====")
        print(v[cols].to_string(index=False))
        best = v[v.IS_trades >= 50].iloc[0]
        print(f"\nCHOSEN in-sample: {best.cfg_id}  ->  test 2019+ CAGR {best.OOS_cagr_pct}%, "
              f"max DD {best.OOS_maxdd_pct}%;  whole period Rs10L -> Rs{best.ALL_end_equity:,.0f}")
        prev = v[v.cfg_id == PREV].iloc[0]
        print(f"Earlier biased-run pick ({PREV}): test CAGR {prev.OOS_cagr_pct}%, max DD {prev.OOS_maxdd_pct}%")
        print(f"Test CAGR across 16 configs: median {v.OOS_cagr_pct.median():.1f}%, "
              f"{int((v.OOS_cagr_pct > 11.3).sum())}/16 beat NIFTY 11.3%")
        acct, eq = keep[(ver, best.cfg_id)]
        print(yearly(acct, eq, mk["capital"]).to_string(index=False))
