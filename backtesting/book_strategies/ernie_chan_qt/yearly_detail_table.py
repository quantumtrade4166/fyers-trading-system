"""
yearly_detail_table.py
Rich year-by-year breakdown: P&L, deployed-capital return, intra-year drawdown,
trades, win rate — for TCS/INFY, NTPC/PG, and the combined portfolio.

Capital: 1 lot = Rs1L  =>  TCS/INFY = Rs2L | NTPC/PG = Rs3L | Both = Rs5L

Drawdown note: uses CLOSED P&L equity (exit-day booking). Open positions are
not marked to market; the drawdown shown is the worst booked equity fall each
year. This is conservative — real-time unrealised drawdown can be deeper.
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

CAP_TI = 200_000
CAP_PG = 300_000
CAP_CB = CAP_TI + CAP_PG
N_YEARS = (END - START).days / 365.25

# ── Load trade logs ───────────────────────────────────────────────────────────
ti = pd.read_csv(BASE / "trades_pairs_tcs_infy_longrun.csv")
pg = pd.read_csv(BASE / "trades_ntpc_powergrid_v2.csv")
for df in [ti, pg]:
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"]  = pd.to_datetime(df["exit_date"])

# Filter TCS/INFY to the common analysis period
ti = ti[ti["entry_date"] >= START].copy()

# ── Build daily P&L (exit-day booking) ───────────────────────────────────────
def build_daily_pnl(trades_df, idx):
    pnl = pd.Series(0.0, index=idx)
    for _, tr in trades_df.iterrows():
        if tr["exit_date"] in pnl.index:
            pnl[tr["exit_date"]] += tr["net_pnl"]
    return pnl

pnl_ti = build_daily_pnl(ti, idx)
pnl_pg = build_daily_pnl(pg, idx)
pnl_cb = pnl_ti + pnl_pg

# ── Cumulative equity curves ──────────────────────────────────────────────────
eq_ti = CAP_TI + pnl_ti.cumsum()
eq_pg = CAP_PG + pnl_pg.cumsum()
eq_cb = CAP_CB + pnl_cb.cumsum()

# ── Build daily deployment flag (True = position is open) ────────────────────
def build_deployed(trades_df, idx):
    dep = pd.Series(False, index=idx)
    for _, tr in trades_df.iterrows():
        mask = (idx >= tr["entry_date"]) & (idx <= tr["exit_date"])
        dep[mask] = True
    return dep

dep_ti = build_deployed(ti, idx)
dep_pg = build_deployed(pg, idx)

# ── Intra-year max drawdown on equity curve ───────────────────────────────────
def intra_year_dd(equity_full, year):
    """Peak-to-trough % drawdown within the given year.
       Uses the rolling peak from the FULL series start so a year starting
       below a prior peak correctly shows a drawdown."""
    yr_mask = equity_full.index.year == year
    if not yr_mask.any():
        return 0.0
    # Rolling max up to end of this year
    peak_full = equity_full.cummax()
    yr_eq     = equity_full[yr_mask]
    yr_peak   = peak_full[yr_mask]
    dd        = (yr_eq - yr_peak) / yr_peak * 100
    return dd.min()

# ── Per-year stats from trade log ─────────────────────────────────────────────
def year_trade_stats(trades_df, year):
    yr = trades_df[trades_df["exit_date"].dt.year == year]
    n    = len(yr)
    wins = int((yr["net_pnl"] > 0).sum())
    pnl  = yr["net_pnl"].sum()
    return n, wins, pnl

# ── Build rows ────────────────────────────────────────────────────────────────
rows = []
for yr in range(2016, 2027):
    yr_idx = idx[idx.year == yr]
    if yr_idx.empty:
        continue

    n_ti, w_ti, pnl_y_ti = year_trade_stats(ti, yr)
    n_pg, w_pg, pnl_y_pg = year_trade_stats(pg, yr)
    pnl_y_cb = pnl_y_ti + pnl_y_pg

    # Deployment fraction (days in position / total business days that year)
    dep_ti_yr = dep_ti[yr_idx].mean()
    dep_pg_yr = dep_pg[yr_idx].mean()
    dep_cb_yr = (dep_ti[yr_idx] | dep_pg[yr_idx]).mean()

    # Average deployed capital for the year
    avg_dep_ti = CAP_TI * dep_ti_yr
    avg_dep_pg = CAP_PG * dep_pg_yr
    avg_dep_cb = avg_dep_ti + avg_dep_pg   # weighted, not OR

    # Return on deployed capital (annual, not compound CAGR since 1 year each)
    ret_ti = pnl_y_ti / avg_dep_ti * 100 if avg_dep_ti > 1 else float("nan")
    ret_pg = pnl_y_pg / avg_dep_pg * 100 if avg_dep_pg > 1 else float("nan")
    ret_cb = pnl_y_cb / avg_dep_cb * 100 if avg_dep_cb > 1 else float("nan")

    # Intra-year max drawdown (closed P&L equity)
    dd_ti = intra_year_dd(eq_ti, yr)
    dd_pg = intra_year_dd(eq_pg, yr)
    dd_cb = intra_year_dd(eq_cb, yr)

    rows.append(dict(
        year=yr,
        pnl_ti=pnl_y_ti, n_ti=n_ti, wr_ti=w_ti/n_ti*100 if n_ti else float("nan"),
        dep_ti=dep_ti_yr*100, ret_ti=ret_ti, dd_ti=dd_ti,
        pnl_pg=pnl_y_pg, n_pg=n_pg, wr_pg=w_pg/n_pg*100 if n_pg else float("nan"),
        dep_pg=dep_pg_yr*100, ret_pg=ret_pg, dd_pg=dd_pg,
        pnl_cb=pnl_y_cb, dep_cb=dep_cb_yr*100,
        ret_cb=ret_cb, dd_cb=dd_cb,
    ))

df = pd.DataFrame(rows)

# ── Print ─────────────────────────────────────────────────────────────────────
W = 138
print("=" * W)
print(f"  YEAR-BY-YEAR PAIRS TRADING BREAKDOWN")
print(f"  1 lot = Rs1L margin | TCS/INFY = Rs2L | NTPC/POWERGRID = Rs3L | Combined = Rs5L")
print("=" * W)

# Column headers
print(f"\n  {'Year':>4}  "
      f"{'── TCS / INFY  (Rs2L) ─────────────────────────':>50}  "
      f"{'── NTPC / POWERGRID  (Rs3L) ─────────────────────':>50}  "
      f"{'── COMBINED  (Rs5L) ───────':>28}")
print(f"  {'':>4}  "
      f"{'P&L':>9} {'Tr':>3} {'Win%':>5} {'Dep%':>5} {'Ret%*':>6} {'MaxDD%':>7}  "
      f"{'P&L':>9} {'Tr':>3} {'Win%':>5} {'Dep%':>5} {'Ret%*':>6} {'MaxDD%':>7}  "
      f"{'P&L':>10} {'Ret%*':>6} {'MaxDD%':>7}")
print("  " + "─" * (W - 2))

total_ti = total_pg = 0
for _, r in df.iterrows():
    yr = int(r["year"])
    total_ti += r["pnl_ti"]
    total_pg += r["pnl_pg"]

    note = ""
    if r["pnl_ti"] < 0 and r["pnl_pg"] > 0: note = "  <- PG cushioned"
    elif r["pnl_pg"] < 0 and r["pnl_ti"] > 0: note = "  <- TI cushioned"
    if yr == 2026: note = "  (Jan-Jun only)" + note.strip()

    def fmt(v, width=6, decimals=1):
        return f"{v:{width}.{decimals}f}" if not np.isnan(v) else f"{'n/a':>{width}}"

    print(f"  {yr:>4}  "
          f"{r['pnl_ti']:>9,.0f} {r['n_ti']:>3} "
          f"{fmt(r['wr_ti'],5,0)}% {r['dep_ti']:>4.0f}% "
          f"{fmt(r['ret_ti'],6,1)}% {r['dd_ti']:>6.1f}%  "
          f"{r['pnl_pg']:>9,.0f} {r['n_pg']:>3} "
          f"{fmt(r['wr_pg'],5,0)}% {r['dep_pg']:>4.0f}% "
          f"{fmt(r['ret_pg'],6,1)}% {r['dd_pg']:>6.1f}%  "
          f"{r['pnl_cb']:>10,.0f} {fmt(r['ret_cb'],6,1)}% {r['dd_cb']:>6.1f}%"
          f"{note}")

print("  " + "─" * (W - 2))
total_cb = total_ti + total_pg

# Averages (excl 2026 partial)
full = df[df["year"] < 2026]
print(f"  {'AVG':>4}  "
      f"{'':>9} {'':>3} {full['wr_ti'].mean():>4.0f}% {full['dep_ti'].mean():>4.0f}% "
      f"{full['ret_ti'].mean():>6.1f}% {'':>7}  "
      f"{'':>9} {'':>3} {full['wr_pg'].mean():>4.0f}% {full['dep_pg'].mean():>4.0f}% "
      f"{full['ret_pg'].mean():>6.1f}% {'':>7}  "
      f"{'':>10} {full['ret_cb'].mean():>6.1f}%")
print(f"  {'TOTAL':>4}  {total_ti:>9,.0f} {df['n_ti'].sum():>3.0f} "
      f"{'':>5} {'':>5} {'':>7} {'':>7}  "
      f"{total_pg:>9,.0f} {df['n_pg'].sum():>3.0f} "
      f"{'':>5} {'':>5} {'':>7} {'':>7}  "
      f"{total_cb:>10,.0f}")

print(f"\n  * Ret% = P&L / (Capital × Dep%) for that year = return on the capital that was ACTUALLY deployed")
print(f"    MaxDD% = peak-to-trough on the BOOKED equity curve (closed trades only); real MTM drawdown is deeper")

print(f"\n{'=' * W}")
print(f"  SUMMARY  (full period 2016-2026, ~{N_YEARS:.1f} years)")
print(f"{'=' * W}")

combos = [
    ("TCS/INFY  (Rs2L)", CAP_TI, total_ti, df["dep_ti"].mean()/100,
     df["wr_ti"].mean(), df["n_ti"].sum()),
    ("NTPC/PG   (Rs3L)", CAP_PG, total_pg, df["dep_pg"].mean()/100,
     df["wr_pg"].mean(), df["n_pg"].sum()),
    ("COMBINED  (Rs5L)", CAP_CB, total_cb,
     (df["dep_ti"].mean()/100 * CAP_TI + df["dep_pg"].mean()/100 * CAP_PG) / CAP_CB,
     float("nan"), df["n_ti"].sum() + df["n_pg"].sum()),
]

print(f"\n  {'Strategy':<20} {'Total P&L':>11} {'Trades':>7} {'Win%':>6} "
      f"{'Committed CAGR':>15} {'Avg Dep%':>9} {'Return on Deployed':>19}")
print("  " + "─" * 95)
for name, cap, pnl, dep_frac, wr, n in combos:
    cagr = ((cap + pnl) / cap) ** (1/N_YEARS) - 1
    avg_dep_cap  = cap * dep_frac
    annual_pnl   = pnl / N_YEARS
    ret_dep = annual_pnl / avg_dep_cap * 100 if avg_dep_cap > 0 else 0
    wr_str = f"{wr:.1f}%" if not np.isnan(wr) else "─"
    print(f"  {name:<20} Rs{pnl:>9,.0f} {n:>7.0f} {wr_str:>6} "
          f"{cagr*100:>13.2f}%  {dep_frac*100:>7.1f}%  {ret_dep:>17.1f}%/yr")

print(f"\n  Profitable years:")
print(f"    TCS/INFY  : {(df['pnl_ti']>0).sum()}/{len(df)} years "
      f"| Best: Rs{df['pnl_ti'].max():,.0f} ({int(df.loc[df['pnl_ti'].idxmax(),'year'])}) "
      f"| Worst: Rs{df['pnl_ti'].min():,.0f} ({int(df.loc[df['pnl_ti'].idxmin(),'year'])})")
print(f"    NTPC/PG   : {(df['pnl_pg']>0).sum()}/{len(df)} years "
      f"| Best: Rs{df['pnl_pg'].max():,.0f} ({int(df.loc[df['pnl_pg'].idxmax(),'year'])}) "
      f"| Worst: Rs{df['pnl_pg'].min():,.0f} ({int(df.loc[df['pnl_pg'].idxmin(),'year'])})")
print(f"    Combined  : {(df['pnl_cb']>0).sum()}/{len(df)} years "
      f"| Best: Rs{df['pnl_cb'].max():,.0f} ({int(df.loc[df['pnl_cb'].idxmax(),'year'])}) "
      f"| Worst: Rs{df['pnl_cb'].min():,.0f} ({int(df.loc[df['pnl_cb'].idxmin(),'year'])})")

print(f"\n  Bad-year insurance (where one pair saved the other):")
for _, r in df[df["year"] < 2026].iterrows():
    yr = int(r["year"])
    if r["pnl_ti"] < 0 and r["pnl_pg"] > 0:
        improvement = r["pnl_cb"] - r["pnl_ti"]
        print(f"    {yr}: TCS/INFY Rs{r['pnl_ti']:,.0f}  +  NTPC/PG Rs{r['pnl_pg']:+,.0f}  "
              f"= Combined Rs{r['pnl_cb']:,.0f}  (Rs{improvement:+,.0f} better)")
    elif r["pnl_pg"] < 0 and r["pnl_ti"] > 0:
        improvement = r["pnl_cb"] - r["pnl_pg"]
        print(f"    {yr}: NTPC/PG Rs{r['pnl_pg']:,.0f}  +  TCS/INFY Rs{r['pnl_ti']:+,.0f}  "
              f"= Combined Rs{r['pnl_cb']:,.0f}  (Rs{improvement:+,.0f} better)")

print()
