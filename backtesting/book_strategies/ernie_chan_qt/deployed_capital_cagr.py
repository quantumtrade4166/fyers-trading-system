"""
deployed_capital_cagr.py
Calculate return on DEPLOYED capital (not committed capital) for each pair
and the combined portfolio.

Key distinction:
  Committed capital = margin always blocked (Rs2L TCS/INFY, Rs3L NTPC/PG)
  Deployed capital  = committed capital x fraction of days in a position
  Idle capital      = sitting unused while waiting for signal

TCS/INFY earlier showed ~30% CAGR on deployed capital.
This script verifies and extends that to NTPC/PG and combined.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path

BASE  = Path("backtesting/book_strategies/ernie_chan_qt/results")
START = pd.Timestamp("2016-01-11")
END   = pd.Timestamp("2026-06-15")
idx   = pd.date_range(START, END, freq="B")
N_YEARS = (END - START).days / 365.25

# ── Trade logs ────────────────────────────────────────────────────────────────
ti = pd.read_csv(BASE / "trades_pairs_tcs_infy_longrun.csv")
pg = pd.read_csv(BASE / "trades_ntpc_powergrid_v2.csv")
for df in [ti, pg]:
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"]  = pd.to_datetime(df["exit_date"])

CAP_TI = 200_000   # Rs2L
CAP_PG = 300_000   # Rs3L

# ── Build daily deployed flags and P&L ───────────────────────────────────────
def build_daily(trades_df, capital, idx, start):
    pnl      = pd.Series(0.0,   index=idx)
    deployed = pd.Series(False, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in pnl.index:
            pnl[tr["exit_date"]] += tr["net_pnl"]
        # Mark every day this position is open as deployed
        mask = (idx >= max(tr["entry_date"], start)) & (idx <= tr["exit_date"])
        deployed[mask] = True
    return pnl, deployed

pnl_ti, dep_ti = build_daily(ti, CAP_TI, idx, START)
pnl_pg, dep_pg = build_daily(pg, CAP_PG, idx, START)

pnl_comb = pnl_ti + pnl_pg
dep_comb = dep_ti | dep_pg   # at least one pair in position

# ── Core metrics ──────────────────────────────────────────────────────────────
def analyse(pnl, deployed, cap, name):
    dep_frac   = deployed.sum() / len(idx)          # fraction of days deployed
    avg_daily_deployed = cap * dep_frac              # average daily capital at work

    total_pnl  = pnl.sum()
    annual_pnl = total_pnl / N_YEARS

    # CAGR on committed capital
    cagr_committed = ((cap + total_pnl) / cap) ** (1 / N_YEARS) - 1

    # Return on deployed capital (annualised)
    # = annual P&L / average daily deployed capital
    ret_deployed = annual_pnl / avg_daily_deployed if avg_daily_deployed > 0 else 0

    return {
        "name":              name,
        "committed":         cap,
        "total_pnl":         total_pnl,
        "annual_pnl":        annual_pnl,
        "dep_frac":          dep_frac * 100,
        "avg_deployed":      avg_daily_deployed,
        "cagr_committed":    cagr_committed * 100,
        "ret_deployed":      ret_deployed * 100,
    }

ri = analyse(pnl_ti, dep_ti, CAP_TI, "TCS / INFY")
rp = analyse(pnl_pg, dep_pg, CAP_PG, "NTPC / POWERGRID")

# For combined: total committed = Rs5L, deployed is weighted sum
avg_dep_comb = CAP_TI * (dep_ti.sum() / len(idx)) + CAP_PG * (dep_pg.sum() / len(idx))
total_pnl_c  = pnl_comb.sum()
annual_pnl_c = total_pnl_c / N_YEARS
cagr_c       = ((CAP_TI + CAP_PG + total_pnl_c) / (CAP_TI + CAP_PG)) ** (1/N_YEARS) - 1
ret_dep_c    = annual_pnl_c / avg_dep_comb

rc = {
    "name":           "COMBINED",
    "committed":      CAP_TI + CAP_PG,
    "total_pnl":      total_pnl_c,
    "annual_pnl":     annual_pnl_c,
    "dep_frac":       dep_comb.sum() / len(idx) * 100,   # at least one active
    "avg_deployed":   avg_dep_comb,
    "cagr_committed": cagr_c * 100,
    "ret_deployed":   ret_dep_c * 100,
}

# ── Print ──────────────────────────────────────────────────────────────────────
sep = "=" * 68
print(sep)
print("  RETURN ON DEPLOYED CAPITAL  —  2016 to 2026  (~10.4 years)")
print(sep)

for r in [ri, rp, rc]:
    print(f"\n  {'─'*60}")
    print(f"  {r['name']}")
    print(f"  {'─'*60}")
    print(f"  Committed capital (margin blocked)   : Rs{r['committed']/1e5:.0f}L")
    print(f"  Total P&L (10.4 years)               : Rs{r['total_pnl']:,.0f}")
    print(f"  Average annual P&L                   : Rs{r['annual_pnl']:,.0f}")
    print(f"  Days with position open              : {r['dep_frac']:.1f}%")
    print(f"  Avg daily capital deployed           : Rs{r['avg_deployed']:,.0f}")
    print(f"  ── Returns ─────────────────────────────────────────────")
    print(f"  CAGR on COMMITTED capital            : {r['cagr_committed']:.2f}%")
    print(f"  CAGR on DEPLOYED capital             : {r['ret_deployed']:.2f}%  <──")

print()
print(sep)
print("  THE KEY COMPARISON")
print(sep)
print()
print(f"  {'Strategy':<22} {'Committed':>10} {'Deployed':>10} {'CAGR(commit)':>13} {'CAGR(deploy)':>13}")
print(f"  {'─'*72}")
for r in [ri, rp, rc]:
    print(f"  {r['name']:<22} Rs{r['committed']/1e5:.0f}L{' ':>7} "
          f"Rs{r['avg_deployed']:>8,.0f}     "
          f"{r['cagr_committed']:>10.2f}%    "
          f"{r['ret_deployed']:>10.2f}%")

print()
print(sep)
print("  WHAT THIS MEANS")
print(sep)
print()
dep_i  = ri["ret_deployed"]
dep_p  = rp["ret_deployed"]
dep_c  = rc["ret_deployed"]
cag_i  = ri["cagr_committed"]
cag_p  = rp["cagr_committed"]
cag_c  = rc["cagr_committed"]

print(f"  Both pairs earn ~{(dep_i+dep_p)/2:.0f}% per year on the capital actually")
print(f"  deployed in positions.  This rate does NOT improve by adding")
print(f"  a second pair — it stays at ~{dep_c:.0f}%.")
print()
print(f"  What DOES improve:")
print(f"    Committed CAGR:   TCS/INFY {cag_i:.1f}%  +  NTPC/PG {cag_p:.1f}%  =  Combined {cag_c:.1f}%")
print(f"    Why same?         Both strategies idle ~62% of the time.")
print(f"                      Adding a 2nd pair doesn't fill that idle time")
print(f"                      much — combined still idle {100-rc['dep_frac']:.0f}% of days.")
print()
print(f"  The REAL gain from combining:")
print(f"    Max Drawdown  : -37% (TCS alone) -> -13% (both)  [from prev analysis]")
print(f"    Sharpe Ratio  : 0.52 (each alone) -> 0.74 (both)")
print(f"    Bad year hedge: 2021 TCS lost Rs61K -> PG earned Rs27K")
print(f"                    2023 PG lost Rs67K -> TCS earned Rs37K")
print()
print(f"  To actually INCREASE the ~{dep_c:.0f}% deployed-capital return,")
print(f"  you need a strategy that finds MORE signals (more trades)")
print(f"  or trades with a higher edge per trade.")
print(f"  Adding more pairs just gives you more of the same ~{dep_c:.0f}%.")
print()

# Year-by-year deployed efficiency
print(sep)
print("  YEAR-BY-YEAR: HOW MUCH OF EACH RS5L WAS WORKING?")
print(sep)
print(f"  {'Year':<6} {'TI_dep%':>8} {'PG_dep%':>8} {'Both_dep%':>10} {'Annual P&L':>12} {'Return on deployed':>20}")
print(f"  {'─'*70}")
for yr in range(2016, 2027):
    ti_yr  = dep_ti[dep_ti.index.year == yr]
    pg_yr  = dep_pg[dep_pg.index.year == yr]
    cb_yr  = dep_comb[dep_comb.index.year == yr]
    pl_yr  = pnl_comb[pnl_comb.index.year == yr].sum()
    ti_pct = ti_yr.mean() * 100
    pg_pct = pg_yr.mean() * 100
    cb_pct = cb_yr.mean() * 100
    avg_dep = CAP_TI * ti_yr.mean() + CAP_PG * pg_yr.mean()
    ret_dep = pl_yr / avg_dep * 100 if avg_dep > 0 else 0
    print(f"  {yr:<6} {ti_pct:>7.0f}% {pg_pct:>7.0f}% {cb_pct:>9.0f}%   "
          f"{pl_yr:>10,.0f}   {ret_dep:>18.1f}%")
