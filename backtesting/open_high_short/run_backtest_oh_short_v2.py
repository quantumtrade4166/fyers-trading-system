import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path
from collections import defaultdict

DATA_DIR          = Path("G:/fyers_data_pipeline/data")
OUT_DIR           = Path(__file__).parent / "results"
OUT_DIR.mkdir(exist_ok=True)

STARTING_CAPITAL  = 1_000_000
DAILY_RISK_PCT    = 0.01          # 1% of equity at risk per day
TRADES_PER_DAY    = 5             # first 5 signals of the day
OH_TOLERANCE      = 0.001         # (High-Open)/Open ≤ 0.1%
SL_BUFFER         = 0.001         # SL = signal High × 1.001
TARGET_RR         = 2.0           # 1:2 RR
START_DATE        = "2026-01-01"
END_DATE          = "2026-06-30"
LAST_ENTRY_HHMM   = 14 * 60 + 30
EOD_HHMM          = 15 * 60 + 15


def load_symbol(sym_dir: Path) -> pd.DataFrame | None:
    frames = []
    for year_dir in sorted(sym_dir.iterdir()):
        f = year_dir / "ohlcv_5min.parquet"
        if f.exists():
            frames.append(pd.read_parquet(f))
    if not frames:
        return None
    df = pd.concat(frames)
    if "datetime" in df.columns:
        df = df.set_index("datetime")
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df[START_DATE:END_DATE]
    return df if len(df) >= 20 else None


def scan_all_signals(all_data: dict) -> list[dict]:
    """
    Phase 1: Scan every symbol and collect ALL potential O=H signals.
    For each signal, pre-simulate the trade outcome.
    Returns a flat list of signal dicts, one entry per potential trade.
    """
    signals = []

    for sym, df in all_data.items():
        o_arr  = df["open"].values.astype(float)
        h_arr  = df["high"].values.astype(float)
        l_arr  = df["low"].values.astype(float)
        c_arr  = df["close"].values.astype(float)
        ts_pd  = df.index
        hhmm   = ts_pd.hour * 60 + ts_pd.minute
        dates  = np.array(ts_pd.date)
        n      = len(df)

        with np.errstate(invalid="ignore", divide="ignore"):
            gap = np.where(o_arr > 0, (h_arr - o_arr) / o_arr, 1.0)

        signal_mask = (gap <= OH_TOLERANCE) & (c_arr < h_arr) & (hhmm <= LAST_ENTRY_HHMM)
        signal_idx  = np.where(signal_mask)[0]

        seen_dates: set = set()          # one signal per stock per day

        for i in signal_idx:
            d = dates[i]
            if d in seen_dates:
                continue
            seen_dates.add(d)

            if i + 1 >= n or dates[i + 1] != d:
                continue

            entry  = o_arr[i + 1]
            if entry <= 0:
                continue

            sl      = h_arr[i] * (1.0 + SL_BUFFER)
            sl_dist = sl - entry
            if sl_dist <= 0:
                continue

            target = entry - TARGET_RR * sl_dist

            # simulate forward on same day
            exit_p = None; exit_r = None; exit_i = None
            j = i + 1
            while j < n and dates[j] == d:
                bh = h_arr[j]; bl = l_arr[j]; bc = c_arr[j]; bt = hhmm[j]
                if bh >= sl:
                    exit_p = sl;     exit_r = "sl";     exit_i = j; break
                if bl <= target:
                    exit_p = target; exit_r = "target"; exit_i = j; break
                if bt >= EOD_HHMM:
                    exit_p = bc;     exit_r = "eod";    exit_i = j; break
                j += 1

            if exit_p is None:
                exit_p = c_arr[min(j - 1, n - 1)]
                exit_r = "eod"
                exit_i = min(j - 1, n - 1)

            signals.append({
                "symbol":      sym.replace("NSE_","").replace("_EQ",""),
                "signal_dt":   df.index[i],
                "entry_dt":    df.index[i + 1],
                "exit_dt":     df.index[exit_i],
                "entry":       round(entry, 2),
                "sl":          round(sl, 2),
                "target":      round(target, 2),
                "sl_dist":     round(sl_dist, 4),
                "exit":        round(exit_p, 2),
                "exit_reason": exit_r,
                "holding_bars": exit_i - i,
            })

    return signals


def run_portfolio(signals: list[dict]) -> dict:
    """
    Phase 2: For each trading day, pick first N signals by entry_dt.
    Risk = DAILY_RISK_PCT of current equity ÷ TRADES_PER_DAY per trade.
    """
    df_sig = pd.DataFrame(signals)
    df_sig["date"] = df_sig["entry_dt"].dt.date
    df_sig = df_sig.sort_values("entry_dt")

    equity      = STARTING_CAPITAL
    trades_out  = []
    daily_equity = {}

    for date, grp in df_sig.groupby("date"):
        top5 = grp.head(TRADES_PER_DAY)

        day_risk     = equity * DAILY_RISK_PCT
        risk_per_trade = day_risk / TRADES_PER_DAY

        day_pnl = 0.0
        for _, row in top5.iterrows():
            sl_dist = row["sl_dist"]
            if sl_dist <= 0:
                continue

            qty = int(risk_per_trade / sl_dist)
            if qty == 0:
                continue

            actual_risk = qty * sl_dist
            pnl         = (row["entry"] - row["exit"]) * qty

            day_pnl += pnl
            trades_out.append({
                "symbol":        row["symbol"],
                "entry_dt":      row["entry_dt"],
                "exit_dt":       row["exit_dt"],
                "entry":         row["entry"],
                "sl":            row["sl"],
                "target":        row["target"],
                "exit":          row["exit"],
                "qty":           qty,
                "risk_rs":       round(actual_risk, 2),
                "pnl":           round(pnl, 2),
                "exit_reason":   row["exit_reason"],
                "holding_bars":  row["holding_bars"],
            })

        equity += day_pnl
        daily_equity[pd.Timestamp(date)] = equity

    eq_s = pd.Series(daily_equity).sort_index()
    # fill weekends/holidays with last value
    full_idx = pd.date_range(START_DATE, END_DATE)
    eq_s     = eq_s.reindex(full_idx).ffill().fillna(STARTING_CAPITAL)

    return {"trades": pd.DataFrame(trades_out), "equity": eq_s}


def print_results(result: dict):
    tr = result["trades"]
    eq = result["equity"]

    if tr.empty:
        print("No trades found.")
        return

    final  = eq.iloc[-1]
    pk     = eq.cummax()
    dd     = (eq - pk) / pk * 100
    years  = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr   = ((final / STARTING_CAPITAL) ** (1 / years) - 1) * 100

    wins   = tr[tr["pnl"] > 0]
    losses = tr[tr["pnl"] <= 0]
    wr     = len(wins) / len(tr) * 100
    gross_w = wins["pnl"].sum()
    gross_l = abs(losses["pnl"].sum())
    pf      = gross_w / gross_l if gross_l else float("inf")
    avg_w   = wins["pnl"].mean() if len(wins) else 0
    avg_l   = losses["pnl"].mean() if len(losses) else 0
    exp     = (wr / 100) * avg_w + (1 - wr / 100) * avg_l

    trading_days = tr["entry_dt"].dt.date.nunique()

    print(f"\n{'='*68}")
    print(f"  OPEN=HIGH SHORT  v2  |  5 trades/day  |  Jan-Jun 2026")
    print(f"  Daily risk 1% equity ÷ 5 = Rs 2k/trade initially")
    print(f"  Entry: next-bar open  |  SL: +0.1% above High  |  Target: 1:2 RR")
    print(f"{'='*68}")
    print(f"  Starting Capital   : Rs {STARTING_CAPITAL:>12,.0f}")
    print(f"  Final Equity       : Rs {final:>12,.0f}")
    print(f"  Net P&L            : Rs {final - STARTING_CAPITAL:>+12,.0f}")
    print(f"  CAGR (annualised)  : {cagr:>8.2f}%")
    print(f"  Max Drawdown       : {dd.min():>8.2f}%")
    print(f"  Trading Days       : {trading_days:>6}")
    print(f"  Total Trades       : {len(tr):>6}  ({len(tr)/trading_days:.1f}/day)")
    print(f"  Win Rate           : {wr:>8.1f}%")
    print(f"  Profit Factor      : {pf:>8.2f}")
    print(f"  Avg Win            : Rs {avg_w:>+10,.0f}")
    print(f"  Avg Loss           : Rs {avg_l:>+10,.0f}")
    print(f"  Expectancy/trade   : Rs {exp:>+10,.0f}")
    print(f"  Avg daily P&L      : Rs {(final-STARTING_CAPITAL)/trading_days:>+10,.0f}")
    print(f"  Avg Hold (bars)    : {tr['holding_bars'].mean():>8.1f}  ({tr['holding_bars'].mean()*5:.0f} mins)")

    print(f"\n  Exit breakdown:")
    for reason, cnt in tr["exit_reason"].value_counts().items():
        pct  = cnt / len(tr) * 100
        ppnl = tr[tr["exit_reason"] == reason]["pnl"].sum()
        print(f"    {reason:<12}: {cnt:>5}  ({pct:4.1f}%)   P&L Rs {ppnl:>+9,.0f}")

    print(f"\n  Monthly P&L:")
    tr["month"] = tr["entry_dt"].dt.to_period("M")
    monthly = tr.groupby("month")["pnl"].agg(["sum", "count", "mean"])
    print(f"  {'Month':<8} {'Trades':>7} {'Avg/trade':>12} {'Monthly P&L':>15}")
    print("  " + "-"*47)
    for period, mrow in monthly.iterrows():
        print(f"  {str(period):<8} {int(mrow['count']):>7}  Rs {mrow['mean']:>+8,.0f}   Rs {mrow['sum']:>+10,.0f}")

    print(f"\n  Weekly equity progression:")
    eq_weekly = eq.resample("W").last()
    prev = STARTING_CAPITAL
    for dt, val in eq_weekly.items():
        chg = val - prev
        pct = chg / prev * 100
        print(f"    {str(dt.date()):<12}  Rs {val:>12,.0f}   week {chg:>+9,.0f}  ({pct:>+5.1f}%)")
        prev = val

    print(f"\n  Top 10 single trades:")
    for _, r in tr.nlargest(10, "pnl").iterrows():
        print(f"    {r['symbol']:<14} {str(r['entry_dt'])[:16]}  "
              f"{r['exit_reason']:<8}  Rs {r['pnl']:>+7,.0f}")

    print(f"\n  Worst 10 single trades:")
    for _, r in tr.nsmallest(10, "pnl").iterrows():
        print(f"    {r['symbol']:<14} {str(r['entry_dt'])[:16]}  "
              f"{r['exit_reason']:<8}  Rs {r['pnl']:>+7,.0f}")
    print()


if __name__ == "__main__":
    sym_dirs = sorted([d for d in DATA_DIR.iterdir()
                       if d.is_dir() and d.name.startswith("NSE_") and d.name.endswith("_EQ")])

    print(f"Loading {len(sym_dirs)} symbols (Jan-Jun 2026)...")
    all_data = {}
    for sym_dir in sym_dirs:
        df = load_symbol(sym_dir)
        if df is not None:
            all_data[sym_dir.name] = df

    print(f"Loaded {len(all_data)} symbols.\nPhase 1: scanning O=H signals...")
    signals = scan_all_signals(all_data)
    print(f"  Found {len(signals)} total signals across all stocks.\n")

    print("Phase 2: portfolio simulation (5 trades/day, 1% daily risk)...")
    result = run_portfolio(signals)

    print_results(result)

    out = OUT_DIR / "oh_short_v2_trades.csv"
    result["trades"].to_csv(out, index=False)
    print(f"  Saved → {out}")
