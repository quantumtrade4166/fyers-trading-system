import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
VCP (Volatility Contraction Pattern) on Nifty 500, DAILY — long only.

    .venv\Scripts\python.exe -m backtesting.patterns.run_vcp_nifty500

Universe : Nifty 500 (today's members — survivorship bias, which flatters a
           buy-the-leaders pattern like VCP), 2005 → 2026-09
Account  : Rs10 lakh, 1% risk per trade on current equity, cash only,
           max 20% of capital per stock, trades that don't fit are skipped
Costs    : 0.15% per side (delivery)
Honesty  : config chosen on in-sample (before 2019) CAGR, reported 2019 →
"""

import time
from pathlib import Path

import pandas as pd

from backtesting.engine.markets import MARKETS
from backtesting.engine.portfolio import simulate, stats
from backtesting.engine.universe import run_universe

MARKET, OUT = "in_stocks_1d", Path(__file__).resolve().parent / "results"
MOD = "backtesting.patterns.vcp"


def grid():
    g = []
    k = 0
    for nc in (2, 3):
        for z in (1.5, 2.5):
            for dr in (0.8, 0):
                for tr in (20, 50):
                    cfg = {"n_c": nc, "z": z, "dry": dr, "trail": tr, "yr": 252,
                           "ma_fast": 50, "ma_slow": 200, "max_wait": 20,
                           "sides": "long", "harness": {}}
                    g.append(("VCP", MOD, "vcp", f"{k}|n_c={nc},z={z},dry={dr},trail={tr}", cfg))
                    k += 1
    return g


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    mk = MARKETS[MARKET]
    t0 = time.time()
    trades = run_universe(grid(), market=MARKET)
    print(f"signals done in {time.time()-t0:.0f}s — {len(trades):,} raw trades")
    trades.to_parquet(OUT / "vcp_nifty500_trades.parquet", index=False)

    rows, eqs = [], {}
    for cid, g in trades.groupby("cfg_id"):
        acct, eq = simulate(g, mk["capital"])
        row = {"cfg_id": cid, "signals": len(g), "skipped_no_cash": int((~acct["taken"]).sum()),
               "avg_hold_days": round(acct.loc[acct.taken, "bars_held"].mean(), 1)}
        for tag, kw in (("IS", {"end": mk["oos_start"]}), ("OOS", {"start": mk["oos_start"]}), ("ALL", {})):
            row.update({f"{tag}_{a}": v for a, v in stats(eq, acct, **kw).items()})
        rows.append(row); eqs[cid] = (acct, eq)
    s = pd.DataFrame(rows)
    s.to_csv(OUT / "vcp_nifty500_all.csv", index=False)

    ok = s[s["IS_trades"] >= 50]
    best = (ok if len(ok) else s).sort_values("IS_cagr_pct", ascending=False).iloc[0]
    pd.set_option("display.width", 260); pd.set_option("display.max_columns", 40)
    cols = ["cfg_id", "IS_trades", "IS_cagr_pct", "IS_maxdd_pct", "OOS_trades", "OOS_win_pct", "OOS_pf",
            "OOS_avgR_gross", "OOS_avgR_net", "OOS_cagr_pct", "OOS_maxdd_pct", "OOS_sharpe", "avg_hold_days"]
    print("\nALL 16 configs:")
    print(s.sort_values("IS_cagr_pct", ascending=False)[cols].to_string(index=False))
    print("\nCHOSEN (best in-sample):", best["cfg_id"])

    acct, eq = eqs[best["cfg_id"]]
    a = acct[acct.taken].copy(); a["year"] = a["exit_time"].dt.year
    yr = []
    for y, g in a.groupby("year"):
        e = eq[eq.index.year == y]
        st = eq[eq.index < e.index[0]].iloc[-1] if (eq.index < e.index[0]).any() else mk["capital"]
        yr.append({"year": y, "trades": len(g), "win%": round((g.pnl_acct > 0).mean()*100),
                   "start_Rs": round(st), "pnl_Rs": round(g.pnl_acct.sum()), "end_Rs": round(e.iloc[-1]),
                   "return_%": round((e.iloc[-1]/st - 1)*100, 1)})
    print(pd.DataFrame(yr).to_string(index=False))
    print(f"\nWhole period: Rs10,00,000 -> Rs{eq.iloc[-1]:,.0f}; best trade Rs{a.pnl_acct.max():+,.0f} "
          f"({a.loc[a.pnl_acct.idxmax(),'symbol']}), worst Rs{a.pnl_acct.min():+,.0f}; "
          f"top-10 trades = {a.pnl_acct.nlargest(10).sum()/a.pnl_acct.sum()*100:.0f}% of total profit")
