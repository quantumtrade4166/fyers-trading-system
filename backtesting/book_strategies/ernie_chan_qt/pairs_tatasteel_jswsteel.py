"""
pairs_tatasteel_jswsteel.py
Pairs trading backtest: TATASTEEL vs JSWSTEEL (Steel sector)
11-year panel: Yahoo Finance 2015-2024 + Fyers 2024-2026

Cointegration: ADF -3.796 (1% level), R²=0.971, HL=69d
Parameters calibrated to this pair:
  LOOKBACK = 138  (2 × 69d half-life)
  LOT_A = 4400  (TATASTEEL, 1 lot)
  LOT_B = 525   (JSWSTEEL,  1 lot; β=0.152 × 4400 = 668 shares needed, 21% imbalance)
  Capital ~ Rs2L (1 TATASTEEL lot + 1 JSWSTEEL lot)
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
LOOKBACK    = 138
ENTRY_Z     = 2.0
EXIT_Z      = 0.5
STOP_Z      = 3.5
ANNUAL_STOP = 80_000   # start conservative; will calibrate from results
MAX_HL_DAYS = 120      # skip if rolling HL > 120d
COOLDOWN    = 5

LOT_A = 4400  # TATASTEEL
LOT_B = 525   # JSWSTEEL (1 lot; slight under-hedge vs ideal 668 shares)

OUTDIR = Path("backtesting/book_strategies/ernie_chan_qt/results")
OUTDIR.mkdir(parents=True, exist_ok=True)

# ── Load data ─────────────────────────────────────────────────────────────────
# screen_pairs.py saved individual pairs — rebuild from Yahoo+Fyers here
from pathlib import Path
PROJECT_ROOT = Path(".").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import yfinance as yf
from backtesting.data_loader import DataLoader
from backtesting.resample import resample_ohlcv

CACHE = Path("backtesting/book_strategies/ernie_chan_qt/data/tatasteel_jswsteel_daily.parquet")
if not CACHE.exists():
    print("Downloading data...")
    yf_data = {}
    for name, ticker in [("TATASTEEL", "TATASTEEL.NS"), ("JSWSTEEL", "JSWSTEEL.NS")]:
        df = yf.download(ticker, start="2015-01-01", end="2024-05-28", auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index).normalize()
        yf_data[name] = df["Close"].rename(name)
    yf_df = pd.DataFrame(yf_data).dropna()

    loader = DataLoader()
    raw = loader.load_many(["NSE:TATASTEEL-EQ", "NSE:JSWSTEEL-EQ"])
    fy_data = {}
    for sym, df in raw.items():
        d = resample_ohlcv(df, "1D"); d.index = d.index.normalize()
        fy_data["TATASTEEL" if "TATASTEEL" in sym else "JSWSTEEL"] = d["close"]
    fy_df = pd.DataFrame(fy_data).dropna()

    cutoff = pd.Timestamp("2024-05-27")
    data   = pd.concat([yf_df[yf_df.index <= cutoff], fy_df[fy_df.index > cutoff]]).sort_index()
    data   = data[~data.index.duplicated(keep="last")].dropna()
    data.to_parquet(CACHE)
    print(f"Saved: {len(data)} rows")
else:
    data = pd.read_parquet(CACHE)

pa, pb, dates = data["TATASTEEL"].values, data["JSWSTEEL"].values, data.index
n = len(data)
print(f"Data: {dates[0].date()} to {dates[-1].date()}  ({n} rows)")
print(f"TATASTEEL range: Rs{pa.min():.0f} – Rs{pa.max():.0f}")
print(f"JSWSTEEL  range: Rs{pb.min():.0f} – Rs{pb.max():.0f}")
print(f"Config: LOOKBACK={LOOKBACK}, ENTRY_Z={ENTRY_Z}, STOP_Z={STOP_Z}, ANNUAL_STOP=Rs{ANNUAL_STOP:,}")

# ── Rolling signals ───────────────────────────────────────────────────────────
zscores    = np.full(n, np.nan)
half_lives = np.full(n, np.nan)

for t in range(LOOKBACK, n):
    wa, wb = pa[t-LOOKBACK:t], pb[t-LOOKBACK:t]
    try:
        _, beta = OLS(wa, add_constant(wb)).fit().params
        spread  = wa - beta * wb
        phi3    = OLS(np.diff(spread), add_constant(spread[:-1])).fit().params[1]
        hl3     = -np.log(2) / np.log(1 + phi3) if phi3 < 0 else 999
        sp_t    = pa[t] - beta * pb[t]
        mu, sigma = spread.mean(), spread.std()
        half_lives[t] = hl3
        zscores[t]    = (sp_t - mu) / sigma if sigma > 0 else 0.0
    except Exception:
        pass

# ── Simulation ────────────────────────────────────────────────────────────────
brokerage    = 0.0003
position     = 0
entry_pa     = entry_pb = 0.0
entry_bar    = 0
year_pnl     = 0.0
current_year = dates[0].year
cooldown_end = 0
trades       = []

def calc_pnl(pos, epa, epb, xpa, xpb):
    gross = ((xpa - epa) * LOT_A - (xpb - epb) * LOT_B) * pos
    costs = (epa*LOT_A + epb*LOT_B + xpa*LOT_A + xpb*LOT_B) * brokerage
    return gross - costs

for t in range(LOOKBACK, n):
    if np.isnan(zscores[t]):
        continue
    if dates[t].year != current_year:
        current_year = dates[t].year
        year_pnl = 0.0

    z, hl = zscores[t], half_lives[t]

    if position != 0:
        exit_reason = None
        if position == 1 and z >= -EXIT_Z:  exit_reason = "z_exit"
        if position ==-1 and z <= +EXIT_Z:  exit_reason = "z_exit"
        if abs(z) >= STOP_Z:                exit_reason = "z_stop"
        if exit_reason:
            net = calc_pnl(position, entry_pa, entry_pb, pa[t], pb[t])
            year_pnl += net
            trades.append(dict(
                entry_date=dates[entry_bar], exit_date=dates[t],
                hold_days=(dates[t]-dates[entry_bar]).days,
                direction="LongTATA" if position==1 else "ShortTATA",
                entry_pa=entry_pa, entry_pb=entry_pb,
                exit_pa=pa[t], exit_pb=pb[t],
                z_entry=round(zscores[entry_bar],3), z_exit=round(z,3),
                exit_reason=exit_reason, net_pnl=round(net,2),
            ))
            position = 0; cooldown_end = t + COOLDOWN
        continue

    if t < cooldown_end: continue
    if year_pnl < -ANNUAL_STOP: continue
    if hl > MAX_HL_DAYS: continue

    if z < -ENTRY_Z:
        position = 1; entry_pa = pa[t]; entry_pb = pb[t]; entry_bar = t
    elif z > +ENTRY_Z:
        position = -1; entry_pa = pa[t]; entry_pb = pb[t]; entry_bar = t

if position != 0:
    t = n-1
    net = calc_pnl(position, entry_pa, entry_pb, pa[t], pb[t])
    trades.append(dict(
        entry_date=dates[entry_bar], exit_date=dates[t],
        hold_days=(dates[t]-dates[entry_bar]).days,
        direction="LongTATA" if position==1 else "ShortTATA",
        entry_pa=entry_pa, entry_pb=entry_pb,
        exit_pa=pa[t], exit_pb=pb[t],
        z_entry=round(zscores[entry_bar],3), z_exit=round(zscores[t],3),
        exit_reason="end_of_data", net_pnl=round(net,2),
    ))

# ── Results ───────────────────────────────────────────────────────────────────
df = pd.DataFrame(trades)
df.to_csv(OUTDIR / "trades_tatasteel_jswsteel.csv", index=False)

SEP = "=" * 62
print(f"\n{SEP}")
print(f"  TATASTEEL / JSWSTEEL — Backtest Results")
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
    avg_win  = df[df["net_pnl"] > 0]["net_pnl"].mean() if wins else 0
    avg_loss = df[df["net_pnl"] < 0]["net_pnl"].mean() if wins < n_trades else 0

    CAP = 200_000
    START, END = df["entry_date"].min(), df["exit_date"].max()
    idx2  = pd.date_range(START, END, freq="B")
    daily = pd.Series(0.0, index=idx2)
    for _, tr in df.iterrows():
        if tr["exit_date"] in daily.index:
            daily[tr["exit_date"]] += tr["net_pnl"]
    equity = CAP + daily.cumsum()
    n_yr   = (END - START).days / 365.25
    cagr   = ((equity.iloc[-1]/CAP)**(1/n_yr) - 1) * 100
    dr     = daily / CAP
    sharpe = (dr.mean()/dr.std()*np.sqrt(252)) if dr.std() > 0 else 0
    max_dd = ((equity - equity.cummax())/equity.cummax()*100).min()

    dep = pd.Series(False, index=idx2)
    for _, tr in df.iterrows():
        dep[(idx2 >= tr["entry_date"]) & (idx2 <= tr["exit_date"])] = True
    dep_pct = dep.mean() * 100
    dep_ret = (net_pnl/n_yr)/(CAP*dep.mean())*100 if dep.mean() > 0 else 0

    print(f"  Period          : {START.date()} to {END.date()}  ({n_yr:.1f} yr)")
    print(f"  Capital         : Rs{CAP:,}  (1 TATASTEEL lot + 1 JSWSTEEL lot)")
    print(f"  Total Trades    : {n_trades}")
    print(f"  Win Rate        : {win_rate:.1f}%  ({wins}W / {n_trades-wins}L)")
    print(f"  Avg Hold        : {avg_hold:.1f} days")
    print(f"  Avg Winner      : Rs{avg_win:,.0f}")
    print(f"  Avg Loser       : Rs{avg_loss:,.0f}")
    print(f"  Profit Factor   : {pf:.2f}")
    print(f"  Net P&L         : Rs{net_pnl:,.0f}")
    print(f"  ── Equity ──────────────────────────────────────────")
    print(f"  CAGR (Rs2L)     : {cagr:.2f}%")
    print(f"  Sharpe          : {sharpe:.3f}")
    print(f"  Max Drawdown    : {max_dd:.2f}%")
    print(f"  Deployment      : {dep_pct:.1f}% of days")
    print(f"  Return/Deployed : {dep_ret:.1f}%/yr")
    print(f"  ── Year-by-year ─────────────────────────────────────")
    df["yr"] = df["exit_date"].dt.year
    for yr, g in df.groupby("yr"):
        p = g["net_pnl"].sum(); w = (g["net_pnl"]>0).sum()
        bar = "+" if p > 0 else "-"
        print(f"    {yr}: {bar}Rs{abs(p):>8,.0f}  ({len(g)} trades, {w}W/{len(g)-w}L)")
    print(f"    TOTAL: Rs{net_pnl:,.0f}")
    avg_loss_t = df[df["net_pnl"]<0]["net_pnl"].mean() if (df["net_pnl"]<0).any() else -5000
    print(f"\n  ── Calibration ──────────────────────────────────────")
    print(f"  Avg trade loss          : Rs{avg_loss_t:,.0f}")
    print(f"  Recommended ANNUAL_STOP : Rs{abs(avg_loss_t)*3:,.0f}")
    print(f"  Stop exits              : {(df['exit_reason']=='z_stop').sum()}/{n_trades}")
    yr_pnl = df.groupby("yr")["net_pnl"].sum()
    fired  = [y for y,p in yr_pnl.items() if p < -ANNUAL_STOP]
    print(f"  Annual stop fired in    : {fired if fired else 'NO years'}")
