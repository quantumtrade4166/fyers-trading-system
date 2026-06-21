"""
pairs_cipla_drreddy.py
Pairs trading backtest: CIPLA vs DRREDDY (Pharma sector)
11-year panel: Yahoo Finance 2015-2024 stitched with Fyers 2024-2026

Parameters calibrated to THIS pair's statistics:
  Half-life: 98.3 days  → LOOKBACK = 196 (2x HL)
  OLS beta:  1.1802     → 6 DRREDDY lots per 1 CIPLA lot (2.2% imbalance)
  Capital:   ~Rs3L      (1 CIPLA lot + 6 DRREDDY lots)

Lot sizes (NSE F&O):
  CIPLA:   650 shares/lot
  DRREDDY: 125 shares/lot  x6 = 750 shares
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

# ── Config ────────────────────────────────────────────────────────────────────
LOOKBACK    = 196      # 2 × half-life of 98.3 days
ENTRY_Z     = 2.0
EXIT_Z      = 0.5
STOP_Z      = 3.5
ANNUAL_STOP = 100_000  # will calibrate after first run; 3x avg trade loss
MAX_HL_DAYS = 150      # skip if rolling HL > 150d (pair temporarily non-stationary)
COOLDOWN    = 5        # bars to wait after exit before re-entering

# Lot quantities
LOT_A = 650   # CIPLA shares (1 lot)
LOT_B = 750   # DRREDDY shares (6 lots × 125)

OUTDIR = Path("backtesting/book_strategies/ernie_chan_qt/results")
OUTDIR.mkdir(parents=True, exist_ok=True)

# ── Load data ─────────────────────────────────────────────────────────────────
cache = Path("backtesting/book_strategies/ernie_chan_qt/data/cipla_drreddy_daily.parquet")
data  = pd.read_parquet(cache).dropna()
pa    = data["CIPLA"].values
pb    = data["DRREDDY"].values
dates = data.index
n     = len(data)

print(f"Data: {dates[0].date()} to {dates[-1].date()}  ({n} rows)")
print(f"Config: LOOKBACK={LOOKBACK}, ENTRY_Z={ENTRY_Z}, STOP_Z={STOP_Z}, ANNUAL_STOP=Rs{ANNUAL_STOP:,}")

# ── Rolling signal computation ────────────────────────────────────────────────
zscores    = np.full(n, np.nan)
betas      = np.full(n, np.nan)
half_lives = np.full(n, np.nan)

for t in range(LOOKBACK, n):
    wa = pa[t - LOOKBACK:t]
    wb = pb[t - LOOKBACK:t]
    X  = add_constant(wb)
    try:
        _, beta = OLS(wa, X).fit().params
        spread  = wa - beta * wb
        # Half-life check
        phi3 = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
        hl3  = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        sp_t = pa[t] - beta * pb[t]
        mu, sigma = spread.mean(), spread.std()
        betas[t]      = beta
        half_lives[t] = hl3
        zscores[t]    = (sp_t - mu) / sigma if sigma > 0 else 0.0
    except Exception:
        pass

# ── Bar-by-bar simulation ─────────────────────────────────────────────────────
# Position:  0 = flat
#            1 = Long CIPLA / Short DRREDDY  (spread too low, expect up)
#           -1 = Short CIPLA / Long DRREDDY  (spread too high, expect down)

position     = 0
entry_pa     = entry_pb = 0.0
entry_bar    = 0
year_pnl     = 0.0
current_year = dates[0].year
cooldown_end = 0
trades       = []

brokerage = 0.0003  # 0.03% each side

def calc_pnl(pos, epa, epb, xpa, xpb):
    if pos == 1:   # long CIPLA, short DRREDDY
        gross = (xpa - epa) * LOT_A + (epb - xpb) * LOT_B
    else:           # short CIPLA, long DRREDDY
        gross = (epa - xpa) * LOT_A + (xpb - epb) * LOT_B
    costs = (epa * LOT_A + epb * LOT_B + xpa * LOT_A + xpb * LOT_B) * brokerage
    return gross - costs

for t in range(LOOKBACK, n):
    if np.isnan(zscores[t]):
        continue

    bar_date = dates[t]
    yr       = bar_date.year

    # Reset annual P&L tracker on Jan 1
    if yr != current_year:
        current_year = yr
        year_pnl = 0.0

    z  = zscores[t]
    hl = half_lives[t]

    # ── Manage open position ─────────────────────────────────────────────────
    if position != 0:
        exit_reason = None

        if position == 1 and z >= -EXIT_Z:   exit_reason = "z_exit"
        if position ==-1 and z <= +EXIT_Z:   exit_reason = "z_exit"
        if abs(z) >= STOP_Z:                  exit_reason = "z_stop"

        if exit_reason:
            net = calc_pnl(position, entry_pa, entry_pb, pa[t], pb[t])
            year_pnl += net
            trades.append(dict(
                entry_date=dates[entry_bar],
                exit_date=bar_date,
                hold_days=(bar_date - dates[entry_bar]).days,
                direction="LongCIPLA" if position == 1 else "ShortCIPLA",
                entry_pa=entry_pa, entry_pb=entry_pb,
                exit_pa=pa[t], exit_pb=pb[t],
                z_entry=zscores[entry_bar],
                z_exit=z,
                exit_reason=exit_reason,
                net_pnl=round(net, 2),
            ))
            position = 0
            cooldown_end = t + COOLDOWN
        continue

    # ── Entry logic ──────────────────────────────────────────────────────────
    if t < cooldown_end:
        continue
    if year_pnl < -ANNUAL_STOP:
        continue
    if hl > MAX_HL_DAYS:
        continue

    if z < -ENTRY_Z:
        position  =  1
        entry_pa  = pa[t]; entry_pb = pb[t]; entry_bar = t
    elif z > +ENTRY_Z:
        position  = -1
        entry_pa  = pa[t]; entry_pb = pb[t]; entry_bar = t

# Close any open position at last bar
if position != 0:
    t = n - 1
    net = calc_pnl(position, entry_pa, entry_pb, pa[t], pb[t])
    year_pnl += net
    trades.append(dict(
        entry_date=dates[entry_bar], exit_date=dates[t],
        hold_days=(dates[t] - dates[entry_bar]).days,
        direction="LongCIPLA" if position == 1 else "ShortCIPLA",
        entry_pa=entry_pa, entry_pb=entry_pb,
        exit_pa=pa[t], exit_pb=pb[t],
        z_entry=zscores[entry_bar], z_exit=zscores[t],
        exit_reason="end_of_data", net_pnl=round(net, 2),
    ))

# ── Results ───────────────────────────────────────────────────────────────────
df = pd.DataFrame(trades)
df.to_csv(OUTDIR / "trades_cipla_drreddy.csv", index=False)

SEP = "=" * 60
print(f"\n{SEP}")
print(f"  CIPLA / DRREDDY — Backtest Results")
print(SEP)

if df.empty:
    print("  No trades generated.")
else:
    net_pnl  = df["net_pnl"].sum()
    n_trades = len(df)
    wins     = (df["net_pnl"] > 0).sum()
    win_rate = wins / n_trades * 100
    gross_w  = df[df["net_pnl"] > 0]["net_pnl"].sum()
    gross_l  = df[df["net_pnl"] < 0]["net_pnl"].sum()
    pf       = gross_w / abs(gross_l) if gross_l != 0 else float("inf")
    avg_hold = df["hold_days"].mean()
    avg_win  = df[df["net_pnl"] > 0]["net_pnl"].mean() if wins > 0 else 0
    avg_loss = df[df["net_pnl"] < 0]["net_pnl"].mean() if wins < n_trades else 0

    # Equity curve for Sharpe + drawdown
    CAP = 300_000
    START = df["entry_date"].min()
    END   = df["exit_date"].max()
    idx2  = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx2)
    for _, tr in df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    equity = CAP + daily.cumsum()
    n_yr   = (END - START).days / 365.25
    cagr   = ((equity.iloc[-1] / CAP) ** (1/n_yr) - 1) * 100
    daily_r = daily / CAP
    sharpe  = (daily_r.mean() / daily_r.std() * np.sqrt(252)) if daily_r.std() > 0 else 0
    roll_max = equity.cummax()
    max_dd   = ((equity - roll_max) / roll_max * 100).min()

    # Deployment
    dep = pd.Series(False, index=idx2)
    for _, tr in df.iterrows():
        dep[(idx2 >= tr["entry_date"]) & (idx2 <= tr["exit_date"])] = True
    dep_pct = dep.mean() * 100
    dep_ret = (net_pnl / n_yr) / (CAP * dep.mean()) * 100 if dep.mean() > 0 else 0

    print(f"  Period           : {START.date()} to {END.date()}  ({n_yr:.1f} yr)")
    print(f"  Capital (Rs3L)   : margin for 1 CIPLA lot + 6 DRREDDY lots")
    print(f"  Total Trades     : {n_trades}")
    print(f"  Win Rate         : {win_rate:.1f}%  ({wins}W / {n_trades - wins}L)")
    print(f"  Avg Hold         : {avg_hold:.1f} days")
    print(f"  Avg Winner       : Rs{avg_win:,.0f}")
    print(f"  Avg Loser        : Rs{avg_loss:,.0f}")
    print(f"  Profit Factor    : {pf:.2f}")
    print(f"  Net P&L (total)  : Rs{net_pnl:,.0f}")
    print(f"  ── Equity curve stats ──────────────────────────────")
    print(f"  CAGR (Rs3L)      : {cagr:.2f}%")
    print(f"  Sharpe           : {sharpe:.3f}")
    print(f"  Max Drawdown     : {max_dd:.2f}%")
    print(f"  Deployment       : {dep_pct:.1f}% of days")
    print(f"  Return/Deployed  : {dep_ret:.1f}%/yr")

    # Annual breakdown
    print(f"\n  {'─'*52}")
    print(f"  Year-by-year P&L:")
    df["exit_year"] = df["exit_date"].dt.year
    for yr, grp in df.groupby("exit_year"):
        yr_pnl  = grp["net_pnl"].sum()
        yr_wins = (grp["net_pnl"] > 0).sum()
        print(f"    {yr}: Rs{yr_pnl:>9,.0f}  ({len(grp)} trades, {yr_wins}W/{len(grp)-yr_wins}L)")
    print(f"    TOTAL: Rs{net_pnl:,.0f}")

    # Calibration hint for ANNUAL_STOP
    avg_loss_trade = df[df["net_pnl"] < 0]["net_pnl"].mean() if (df["net_pnl"] < 0).any() else -10000
    print(f"\n  ── Calibration hints ───────────────────────────────")
    print(f"  Avg loss per trade       : Rs{avg_loss_trade:,.0f}")
    print(f"  Recommended ANNUAL_STOP  : Rs{abs(avg_loss_trade)*3:,.0f}  (3× avg trade loss)")
    print(f"  Stop-loss exits          : {(df['exit_reason']=='z_stop').sum()}/{n_trades}")
    print(f"  Annual stop fired years  : checking...")
    yearly_pnl_check = df.groupby("exit_year")["net_pnl"].sum()
    stop_fired_years = [yr for yr, p in yearly_pnl_check.items() if p < -ANNUAL_STOP]
    print(f"    Current ANNUAL_STOP Rs{ANNUAL_STOP:,}: would fire in {stop_fired_years if stop_fired_years else 'NO years'}")
