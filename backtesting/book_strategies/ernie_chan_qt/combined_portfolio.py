"""
combined_portfolio.py
Compare CAGR of TCS/INFY alone vs NTPC/POWERGRID alone vs both combined.
Uses actual trade logs from both backtests.
Capital: 1 lot = Rs1L margin (user confirmed).
  TCS/INFY    : 1 lot TCS + 1 lot INFY = Rs2L
  NTPC/PGRID  : 1 lot NTPC + 2 lots PG = Rs3L
  Combined    : Rs5L
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path

BASE = Path("backtesting/book_strategies/ernie_chan_qt/results")

# ── Load trade logs ────────────────────────────────────────────────────────────
ti = pd.read_csv(BASE / "trades_pairs_tcs_infy_longrun.csv")
pg = pd.read_csv(BASE / "trades_ntpc_powergrid_v2.csv")

ti["entry_date"] = pd.to_datetime(ti["entry_date"])
ti["exit_date"]  = pd.to_datetime(ti["exit_date"])
pg["entry_date"] = pd.to_datetime(pg["entry_date"])
pg["exit_date"]  = pd.to_datetime(pg["exit_date"])

# ── Capital assumptions ────────────────────────────────────────────────────────
CAP_TI = 200_000    # Rs2L  (TCS 1 lot + INFY 1 lot)
CAP_PG = 300_000    # Rs3L  (NTPC 1 lot + POWERGRID 2 lots)
CAP_COMBINED = CAP_TI + CAP_PG   # Rs5L

# ── Build daily P&L series ─────────────────────────────────────────────────────
# Common period: from when NTPC/PG goes live (2016-01-11) to last data
START = pd.Timestamp("2016-01-11")
END   = pd.Timestamp("2026-06-15")
idx   = pd.date_range(START, END, freq="B")   # business days

daily_ti = pd.Series(0.0, index=idx)
daily_pg = pd.Series(0.0, index=idx)

# TCS/INFY daily P&L (exit day booking)
for _, tr in ti.iterrows():
    if tr["exit_date"] in daily_ti.index:
        daily_ti[tr["exit_date"]] += tr["net_pnl"]

# NTPC/PG daily P&L (exit day booking)
for _, tr in pg.iterrows():
    if tr["exit_date"] in daily_pg.index:
        daily_pg[tr["exit_date"]] += tr["net_pnl"]

daily_combined = daily_ti + daily_pg

# ── Equity curves ──────────────────────────────────────────────────────────────
eq_ti   = CAP_TI       + daily_ti.cumsum()
eq_pg   = CAP_PG       + daily_pg.cumsum()
eq_comb = CAP_COMBINED + daily_combined.cumsum()

# ── CAGR helper ───────────────────────────────────────────────────────────────
def cagr(start_cap, end_cap, n_years):
    return (end_cap / start_cap) ** (1 / n_years) - 1

n_years = (END - START).days / 365.25

# ── Sharpe helper ─────────────────────────────────────────────────────────────
def sharpe(daily_pnl, capital):
    r = daily_pnl / capital
    return (r.mean() / r.std()) * np.sqrt(252) if r.std() > 0 else 0.0

# ── Max drawdown helper ────────────────────────────────────────────────────────
def max_dd(equity):
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max * 100
    return dd.min()

# ── Deployment rate ───────────────────────────────────────────────────────────
def deployment_days(trades_df, idx):
    deployed = pd.Series(False, index=idx)
    for _, tr in trades_df.iterrows():
        mask = (idx >= tr["entry_date"]) & (idx <= tr["exit_date"])
        deployed[mask] = True
    return deployed.sum(), deployed.sum() / len(idx) * 100

ti_dep_days, ti_dep_pct = deployment_days(ti[ti["entry_date"] >= START], idx)
pg_dep_days, pg_dep_pct = deployment_days(pg, idx)

# Combined: at least one pair deployed
ti_dep = pd.Series(False, index=idx)
for _, tr in ti[ti["entry_date"] >= START].iterrows():
    ti_dep[(idx >= tr["entry_date"]) & (idx <= tr["exit_date"])] = True

pg_dep = pd.Series(False, index=idx)
for _, tr in pg.iterrows():
    pg_dep[(idx >= tr["entry_date"]) & (idx <= tr["exit_date"])] = True

both_dep = ti_dep | pg_dep
comb_dep_pct = both_dep.sum() / len(idx) * 100

# ── Print summary ──────────────────────────────────────────────────────────────
sep = "=" * 60
print(sep)
print("  PORTFOLIO COMPARISON  (2016-Jan to 2026-Jun, ~10.4 years)")
print(sep)
print(f"  {'Metric':<32} {'TCS/INFY':>10} {'NTPC/PG':>10} {'COMBINED':>10}")
print(f"  {'-'*60}")
print(f"  {'Capital committed':<32} {'Rs2L':>10} {'Rs3L':>10} {'Rs5L':>10}")

pnl_ti   = daily_ti.sum()
pnl_pg   = daily_pg.sum()
pnl_comb = daily_combined.sum()

cagr_ti   = cagr(CAP_TI,   eq_ti.iloc[-1],   n_years) * 100
cagr_pg   = cagr(CAP_PG,   eq_pg.iloc[-1],   n_years) * 100
cagr_comb = cagr(CAP_COMBINED, eq_comb.iloc[-1], n_years) * 100

print(f"  {'Net P&L (2016-2026)':<32} {pnl_ti:>10,.0f} {pnl_pg:>10,.0f} {pnl_comb:>10,.0f}")
print(f"  {'Final equity':<32} {eq_ti.iloc[-1]:>10,.0f} {eq_pg.iloc[-1]:>10,.0f} {eq_comb.iloc[-1]:>10,.0f}")
print(f"  {'CAGR %':<32} {cagr_ti:>9.2f}% {cagr_pg:>9.2f}% {cagr_comb:>9.2f}%")
print(f"  {'Sharpe Ratio':<32} {sharpe(daily_ti, CAP_TI):>10.3f} "
      f"{sharpe(daily_pg, CAP_PG):>10.3f} {sharpe(daily_combined, CAP_COMBINED):>10.3f}")
print(f"  {'Max Drawdown %':<32} {max_dd(eq_ti):>9.2f}% "
      f"{max_dd(eq_pg):>9.2f}% {max_dd(eq_comb):>9.2f}%")
print(f"  {'Deployed days (capital in use)':<32} {ti_dep_pct:>9.1f}% "
      f"{pg_dep_pct:>9.1f}% {comb_dep_pct:>9.1f}%")

print()
print(sep)
print("  YEAR-BY-YEAR P&L BREAKDOWN")
print(sep)
print(f"  {'Year':<6} {'TCS/INFY':>10} {'NTPC/PG':>10} {'COMBINED':>12}  {'Better with both?':>20}")
print(f"  {'-'*65}")

ti_yr  = daily_ti.groupby(daily_ti.index.year).sum()
pg_yr  = daily_pg.groupby(daily_pg.index.year).sum()

for yr in range(2016, 2027):
    t = ti_yr.get(yr, 0)
    p = pg_yr.get(yr, 0)
    c = t + p
    # Is combined better than the worse of the two?
    worse_individual = min(t, p)
    note = "diversified" if (t < 0 or p < 0) and c > worse_individual else ""
    print(f"  {yr:<6} {t:>10,.0f} {p:>10,.0f} {c:>12,.0f}  {note}")

print(f"  {'TOTAL':<6} {pnl_ti:>10,.0f} {pnl_pg:>10,.0f} {pnl_comb:>12,.0f}")

print()
print(sep)
print("  WHAT DOES Rs5L BUY YOU?")
print(sep)
print(f"  Both pairs on Rs5L capital:")
print(f"    CAGR            : {cagr_comb:.2f}%")
print(f"    Net P&L 10yr    : Rs{pnl_comb:,.0f}")
print(f"    Rs5L grows to   : Rs{eq_comb.iloc[-1]/1e5:.2f}L in ~10 years")
print()
print(f"  Same Rs5L in only TCS/INFY (2.5x scale = 2.5 lots each):")
pnl_ti_scaled = pnl_ti * (500_000 / 200_000)
cagr_ti_scaled = cagr(500_000, 500_000 + pnl_ti_scaled, n_years) * 100
print(f"    CAGR            : {cagr_ti_scaled:.2f}%  (scaling doesn't change CAGR)")
print()
print(f"  Same Rs5L in only NTPC/PG (1.67x scale):")
pnl_pg_scaled = pnl_pg * (500_000 / 300_000)
cagr_pg_scaled = cagr(500_000, 500_000 + pnl_pg_scaled, n_years) * 100
print(f"    CAGR            : {cagr_pg_scaled:.2f}%  (scaling doesn't change CAGR)")
print()
print(f"  Key insight:")
print(f"    TCS/INFY deployed  : {ti_dep_pct:.1f}% of days")
print(f"    NTPC/PG deployed   : {pg_dep_pct:.1f}% of days")
print(f"    Combined (either)  : {comb_dep_pct:.1f}% of days")
print(f"    Both idle at once  : {100 - comb_dep_pct:.1f}% of days")
