import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
import numpy as np
from pathlib import Path

DATA_DIR = Path("G:/fyers_data_pipeline/Nifty 500 Daily Data")

# ── Strategy Parameters ────────────────────────────────────────
ENTRY_LOOKBACK  = 55    # buy breakout above 55-day high
EXIT_LOOKBACK   = 20    # trail stop: close below 20-day low
POSITION_SIZE   = 10_000  # fixed ₹10,000 per trade
MAX_POSITIONS   = 100
STARTING_CAPITAL = 10_00_000

START_DATE = "2005-01-01"
END_DATE   = "2026-06-19"


def load_symbol(symbol: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df[(df.index >= START_DATE) & (df.index <= END_DATE)]
    if len(df) < ENTRY_LOOKBACK + 10:
        return None
    return df


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # Previous N-day high/low (shift so no lookahead on today's bar)
    df["high55"]  = df["high"].shift(1).rolling(ENTRY_LOOKBACK).max()
    df["low20"]   = df["low"].shift(1).rolling(EXIT_LOOKBACK).min()
    # Signal: today's close breaks above 55-day high
    df["entry_signal"] = df["close"] > df["high55"]
    # Exit: today's close drops below 20-day low
    df["exit_signal"]  = df["close"] < df["low20"]
    return df.dropna(subset=["high55", "low20"])


def run_backtest(symbols: list[str], verbose: bool = False) -> dict:
    # Load and compute signals for all symbols
    all_data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = load_symbol(sym)
        if df is None:
            continue
        df = compute_signals(df)
        if len(df) < 10:
            continue
        all_data[sym] = df

    # Build unified date index
    all_dates = sorted(set().union(*[set(df.index) for df in all_data.values()]))

    # ── Portfolio State ────────────────────────────────────────
    open_positions: dict[str, dict] = {}   # symbol → {shares, entry_price, entry_date}
    trades: list[dict] = []
    daily_equity: list[dict] = []

    cash = STARTING_CAPITAL

    for date in all_dates:
        # ── Exits first ──────────────────────────────────────
        to_exit = []
        for sym, pos in open_positions.items():
            if sym not in all_data:
                continue
            df = all_data[sym]
            if date not in df.index:
                continue
            row = df.loc[date]
            if row["exit_signal"]:
                to_exit.append(sym)

        for sym in to_exit:
            pos = open_positions.pop(sym)
            df = all_data[sym]
            # Exit at next day's open — find next available date
            sym_dates = df.index.tolist()
            idx = sym_dates.index(date)
            if idx + 1 < len(sym_dates):
                exit_price = df.iloc[idx + 1]["open"]
            else:
                exit_price = df.loc[date]["close"]  # last bar fallback

            proceeds = pos["shares"] * exit_price
            cash += proceeds
            pnl = proceeds - (pos["shares"] * pos["entry_price"])

            trades.append({
                "symbol":       sym,
                "entry_date":   pos["entry_date"],
                "exit_date":    date,
                "entry_price":  pos["entry_price"],
                "exit_price":   exit_price,
                "shares":       pos["shares"],
                "pnl":          pnl,
                "return_pct":   pnl / (pos["shares"] * pos["entry_price"]) * 100,
            })
            if verbose:
                print(f"  EXIT  {sym:15s} entry={pos['entry_price']:.2f} "
                      f"exit={exit_price:.2f} pnl={pnl:+.0f}")

        # ── Entries ──────────────────────────────────────────
        slots_free = MAX_POSITIONS - len(open_positions)
        if slots_free > 0:
            candidates = []
            for sym, df in all_data.items():
                if sym in open_positions:
                    continue
                if date not in df.index:
                    continue
                row = df.loc[date]
                if row["entry_signal"]:
                    candidates.append((sym, df, date))

            # If more candidates than slots, take first N (stable sort by symbol name)
            candidates = candidates[:slots_free]

            for sym, df, sig_date in candidates:
                sym_dates = df.index.tolist()
                idx = sym_dates.index(sig_date)
                if idx + 1 >= len(sym_dates):
                    continue
                entry_price = df.iloc[idx + 1]["open"]
                if entry_price <= 0:
                    continue
                shares = int(POSITION_SIZE / entry_price)
                if shares == 0:
                    continue
                cost = shares * entry_price
                if cost > cash:
                    continue

                cash -= cost
                open_positions[sym] = {
                    "shares":      shares,
                    "entry_price": entry_price,
                    "entry_date":  df.index[idx + 1],
                }
                if verbose:
                    print(f"  ENTRY {sym:15s} price={entry_price:.2f} "
                          f"shares={shares} cost={cost:.0f}")

        # ── Mark-to-market equity ─────────────────────────────
        open_value = 0
        for sym, pos in open_positions.items():
            df = all_data[sym]
            if date in df.index:
                open_value += pos["shares"] * df.loc[date]["close"]

        equity = cash + open_value
        daily_equity.append({"date": date, "equity": equity,
                              "open_positions": len(open_positions)})

    # Close any remaining open positions at last available close
    for sym, pos in open_positions.items():
        df = all_data[sym]
        last_price = df["close"].iloc[-1]
        proceeds = pos["shares"] * last_price
        cash += proceeds
        pnl = proceeds - (pos["shares"] * pos["entry_price"])
        trades.append({
            "symbol":       sym,
            "entry_date":   pos["entry_date"],
            "exit_date":    df.index[-1],
            "entry_price":  pos["entry_price"],
            "exit_price":   last_price,
            "shares":       pos["shares"],
            "pnl":          pnl,
            "return_pct":   pnl / (pos["shares"] * pos["entry_price"]) * 100,
        })

    equity_df = pd.DataFrame(daily_equity).set_index("date")
    trades_df  = pd.DataFrame(trades) if trades else pd.DataFrame()

    return {"equity": equity_df, "trades": trades_df, "final_cash": cash}


def print_results(result: dict):
    eq   = result["equity"]
    tr   = result["trades"]

    final_equity = eq["equity"].iloc[-1]
    peak         = eq["equity"].cummax()
    drawdown     = (eq["equity"] - peak) / peak * 100
    max_dd       = drawdown.min()

    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr  = ((final_equity / STARTING_CAPITAL) ** (1 / years) - 1) * 100

    print("\n" + "=" * 60)
    print("  55-DAY BREAKOUT STRATEGY — BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Period          : {eq.index[0].date()} → {eq.index[-1].date()} ({years:.1f} yrs)")
    print(f"  Starting Capital: ₹{STARTING_CAPITAL:,.0f}")
    print(f"  Final Equity    : ₹{final_equity:,.0f}")
    print(f"  Net P&L         : ₹{final_equity - STARTING_CAPITAL:+,.0f}")
    print(f"  CAGR            : {cagr:.2f}%")
    print(f"  Max Drawdown    : {max_dd:.2f}%")
    print("-" * 60)

    if not tr.empty:
        wins     = tr[tr["pnl"] > 0]
        losses   = tr[tr["pnl"] <= 0]
        win_rate = len(wins) / len(tr) * 100
        avg_win  = wins["pnl"].mean() if len(wins) else 0
        avg_loss = losses["pnl"].mean() if len(losses) else 0
        pf       = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) else float("inf")
        avg_hold = (tr["exit_date"] - tr["entry_date"]).dt.days.mean()

        print(f"  Total Trades    : {len(tr)}")
        print(f"  Win Rate        : {win_rate:.1f}%")
        print(f"  Avg Win         : ₹{avg_win:,.0f}")
        print(f"  Avg Loss        : ₹{avg_loss:,.0f}")
        print(f"  Profit Factor   : {pf:.2f}")
        print(f"  Avg Hold (days) : {avg_hold:.1f}")
        print(f"  Max Positions   : {eq['open_positions'].max()}")
        print(f"  Avg Positions   : {eq['open_positions'].mean():.1f}")

    print("=" * 60)

    # Year-by-year breakdown
    print("\n  Year-by-Year P&L:")
    print(f"  {'Year':<6} {'Start':>12} {'End':>12} {'P&L':>12} {'Return':>8}")
    print("  " + "-" * 54)
    eq_annual = eq["equity"].resample("YE").last()
    eq_annual_start = eq["equity"].resample("YE").first()
    for yr, end_val in eq_annual.items():
        start_val = eq_annual_start[yr]
        pnl_yr = end_val - start_val
        ret_yr = pnl_yr / start_val * 100
        print(f"  {yr.year:<6} ₹{start_val:>10,.0f} ₹{end_val:>10,.0f} "
              f"₹{pnl_yr:>+10,.0f} {ret_yr:>+7.1f}%")

    print()
