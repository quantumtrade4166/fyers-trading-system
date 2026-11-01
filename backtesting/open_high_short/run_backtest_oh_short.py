import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR         = Path("G:/fyers_data_pipeline/data")
OUT_DIR          = Path(__file__).parent / "results"
OUT_DIR.mkdir(exist_ok=True)

STARTING_CAPITAL  = 1_000_000
RISK_PCT          = 0.01
MAX_POSITION_VAL  = 200_000        # Rs 2L max per trade (20% of capital)
OH_TOLERANCE      = 0.001          # (High - Open)/Open ≤ 0.1%
SL_BUFFER         = 0.001          # SL = High × 1.001
TARGET_RR         = 2.0
START_DATE        = "2026-01-01"
END_DATE          = "2026-06-30"
LAST_ENTRY_HHMM   = 14 * 60 + 30  # 14:30
EOD_HHMM          = 15 * 60 + 15  # 15:15


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
    if len(df) < 20:
        return None
    return df


def scan_symbol_fast(sym: str, df: pd.DataFrame) -> list[dict]:
    """Vectorized scan using numpy arrays."""
    ts_arr   = df.index.values          # numpy datetime64
    o_arr    = df["open"].values.astype(float)
    h_arr    = df["high"].values.astype(float)
    l_arr    = df["low"].values.astype(float)
    c_arr    = df["close"].values.astype(float)
    n        = len(ts_arr)

    # precompute minute-of-day for each bar
    ts_pd  = df.index
    hhmm   = ts_pd.hour * 60 + ts_pd.minute

    # date array (integer days for fast grouping)
    dates  = ts_pd.date
    date_arr = np.array(dates)

    # vectorised signal mask
    with np.errstate(invalid="ignore", divide="ignore"):
        gap  = np.where(o_arr > 0, (h_arr - o_arr) / o_arr, 1.0)
    signal_mask = (gap <= OH_TOLERANCE) & (c_arr < h_arr) & (hhmm <= LAST_ENTRY_HHMM)

    signal_indices = np.where(signal_mask)[0]

    trades = []
    traded_dates: set = set()          # one trade per stock per day

    for i in signal_indices:
        # entry bar = next bar on SAME day
        if i + 1 >= n:
            continue
        if date_arr[i + 1] != date_arr[i]:
            continue

        # one trade per stock per calendar day
        day = date_arr[i]
        if day in traded_dates:
            continue
        traded_dates.add(day)

        entry_price = o_arr[i + 1]      # next bar's open
        if entry_price <= 0:
            continue

        sl      = h_arr[i] * (1.0 + SL_BUFFER)
        sl_dist = sl - entry_price
        if sl_dist <= 0:
            continue

        risk_rs      = STARTING_CAPITAL * RISK_PCT
        qty_by_risk  = int(risk_rs / sl_dist)
        qty_by_cap   = int(MAX_POSITION_VAL / entry_price)
        qty          = min(qty_by_risk, qty_by_cap)
        if qty == 0:
            continue
        actual_risk  = qty * sl_dist    # may be less than Rs 10k if capped

        target = entry_price - TARGET_RR * sl_dist

        # scan forward on same day from entry bar
        exit_price  = None
        exit_reason = None
        exit_idx    = None

        j = i + 1
        while j < n and date_arr[j] == date_arr[i]:
            bhi = h_arr[j]
            blo = l_arr[j]
            bc  = c_arr[j]
            bt  = hhmm[j]

            if bhi >= sl:
                exit_price  = sl
                exit_reason = "sl"
                exit_idx    = j
                break
            if blo <= target:
                exit_price  = target
                exit_reason = "target"
                exit_idx    = j
                break
            if bt >= EOD_HHMM:
                exit_price  = bc
                exit_reason = "eod"
                exit_idx    = j
                break
            j += 1

        if exit_price is None:
            # reached end of day without explicit exit (rare edge case)
            exit_price  = c_arr[min(j - 1, n - 1)]
            exit_reason = "eod"
            exit_idx    = min(j - 1, n - 1)

        pnl = (entry_price - exit_price) * qty

        trades.append({
            "symbol":        sym,
            "signal_dt":     str(ts_arr[i])[:19],
            "entry_dt":      str(ts_arr[i + 1])[:19],
            "exit_dt":       str(ts_arr[exit_idx])[:19],
            "signal_open":   round(o_arr[i], 2),
            "signal_high":   round(h_arr[i], 2),
            "signal_close":  round(c_arr[i], 2),
            "entry":         round(entry_price, 2),
            "sl":            round(sl, 2),
            "target":        round(target, 2),
            "exit":          round(exit_price, 2),
            "qty":           qty,
            "risk_rs":       round(actual_risk, 2),
            "pnl":           round(pnl, 2),
            "exit_reason":   exit_reason,
            "holding_bars":  exit_idx - i,
        })

    return trades


def print_results(all_trades: list[dict]):
    if not all_trades:
        print("No trades found.")
        return

    tr = pd.DataFrame(all_trades)
    tr["exit_dt"]  = pd.to_datetime(tr["exit_dt"])
    tr["exit_date"] = tr["exit_dt"].dt.date

    # daily equity
    daily = tr.groupby("exit_date")["pnl"].sum()
    idx   = pd.date_range(START_DATE, END_DATE)
    eq    = pd.Series(0.0, index=idx)
    for d, v in daily.items():
        t = pd.Timestamp(d)
        if t in eq.index:
            eq[t] = v
    eq = eq.cumsum() + STARTING_CAPITAL

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

    print(f"\n{'='*65}")
    print(f"  OPEN=HIGH SHORT  |  5-min  |  Jan-Jun 2026")
    print(f"  Entry: next-bar open | SL: +0.1% above High | Target: 1:2 RR")
    print(f"  Risk: 1% of Rs 10L = Rs {STARTING_CAPITAL*RISK_PCT:,.0f}/trade")
    print(f"{'='*65}")
    print(f"  Starting Capital   : Rs {STARTING_CAPITAL:>12,.0f}")
    print(f"  Final Equity       : Rs {final:>12,.0f}")
    print(f"  Net P&L            : Rs {final - STARTING_CAPITAL:>+12,.0f}")
    print(f"  CAGR (annualised)  : {cagr:>8.2f}%")
    print(f"  Max Drawdown       : {dd.min():>8.2f}%")
    print(f"  Total Trades       : {len(tr):>6}")
    print(f"  Unique Symbols     : {tr['symbol'].nunique():>6}")
    print(f"  Win Rate           : {wr:>8.1f}%")
    print(f"  Profit Factor      : {pf:>8.2f}")
    print(f"  Avg Win            : Rs {avg_w:>+10,.0f}")
    print(f"  Avg Loss           : Rs {avg_l:>+10,.0f}")
    print(f"  Expectancy/trade   : Rs {exp:>+10,.0f}")
    print(f"  Avg Hold (bars)    : {tr['holding_bars'].mean():>8.1f}  "
          f"({tr['holding_bars'].mean()*5:.0f} mins)")

    print(f"\n  Exit breakdown:")
    for reason, cnt in tr["exit_reason"].value_counts().items():
        pct  = cnt / len(tr) * 100
        ppnl = tr[tr["exit_reason"] == reason]["pnl"].sum()
        print(f"    {reason:<12}: {cnt:>5}  ({pct:4.1f}%)   P&L Rs {ppnl:>+10,.0f}")

    print(f"\n  Monthly P&L:")
    tr["month"] = tr["exit_dt"].dt.to_period("M")
    monthly = tr.groupby("month")["pnl"].agg(["sum", "count"])
    for period, mrow in monthly.iterrows():
        print(f"    {str(period):<8}  {int(mrow['count']):>5} trades   Rs {mrow['sum']:>+10,.0f}")

    print(f"\n  Top 10 winners:")
    for _, r in tr.nlargest(10, "pnl").iterrows():
        print(f"    {r['symbol']:<14} {r['entry_dt']}  "
              f"{r['exit_reason']:<8}  Rs {r['pnl']:>+9,.0f}  qty={int(r['qty'])}")

    print(f"\n  Top 10 losers:")
    for _, r in tr.nsmallest(10, "pnl").iterrows():
        print(f"    {r['symbol']:<14} {r['entry_dt']}  "
              f"{r['exit_reason']:<8}  Rs {r['pnl']:>+9,.0f}  qty={int(r['qty'])}")
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

    print(f"Loaded {len(all_data)} symbols.  Scanning O=H signals...\n")

    all_trades = []
    for sym, df in all_data.items():
        t = scan_symbol_fast(sym, df)
        all_trades.extend(t)

    print(f"Total signal trades: {len(all_trades)}")
    print_results(all_trades)

    if all_trades:
        out = OUT_DIR / "oh_short_trades.csv"
        pd.DataFrame(all_trades).to_csv(out, index=False)
        print(f"  Saved → {out}")
