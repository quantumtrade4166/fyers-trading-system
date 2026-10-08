"""
Run the Supertrend (1H) credit-spread backtest, both roll-forward variants,
and print/save a summary.

Usage:
    G:\\fyers_data_pipeline\\.venv\\Scripts\\python.exe backtesting/options_credit_spread/run_backtest.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backtesting.options_credit_spread.data_loader import OptionsDataLoader
from backtesting.options_credit_spread.expiry_calendar import ExpiryCalendar
from backtesting.options_credit_spread.supertrend_signals import build_hourly_supertrend, extract_flip_events
from backtesting.options_credit_spread.strategy import run_backtest, LOT_SIZE

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def summarize(trades: list, label: str) -> dict:
    if not trades:
        print(f"\n=== {label}: no trades ===")
        return {}

    df = pd.DataFrame(trades)
    df = df.sort_values("exit_time").reset_index(drop=True)
    df["cum_pnl"] = df["pnl_rupees"].cumsum()
    df["running_peak"] = df["cum_pnl"].cummax()
    df["drawdown"] = df["cum_pnl"] - df["running_peak"]

    total_pnl = df["pnl_rupees"].sum()
    win_rate = (df["pnl_rupees"] > 0).mean() * 100
    avg_pnl = df["pnl_rupees"].mean()
    max_dd = df["drawdown"].min()
    n = len(df)

    print(f"\n=== {label} ===")
    print(f"Trades: {n} | Win rate: {win_rate:.1f}% | Total P&L: Rs {total_pnl:,.0f} | Avg P&L/trade: Rs {avg_pnl:,.0f}")
    print(f"Max drawdown (cumulative P&L): Rs {max_dd:,.0f}")
    print("Exit reason breakdown:")
    print(df["exit_reason"].value_counts().to_string())
    print("P&L by exit reason:")
    print(df.groupby("exit_reason")["pnl_rupees"].agg(["count", "sum", "mean"]).to_string())
    n_fallback_hedge = df["used_fallback_hedge"].sum() if "used_fallback_hedge" in df else 0
    print(f"Trades using fallback hedge (no strike met <50% premium rule): {n_fallback_hedge}")
    n_approx = df["approx_used"].sum() if "approx_used" in df else 0
    approx_pnl = df.loc[df["approx_used"], "pnl_rupees"].sum() if "approx_used" in df else 0
    print(f"Trades with any off-grid approximation: {n_approx} (their total P&L: Rs {approx_pnl:,.0f})")

    return {
        "label": label, "trades": n, "win_rate": win_rate, "total_pnl": total_pnl,
        "avg_pnl": avg_pnl, "max_dd": max_dd, "df": df,
    }


def main():
    print("Loading spot series and building 1H Supertrend...")
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    print(f"Spot series: {len(spot):,} minutes, {spot.index.min()} to {spot.index.max()}")

    hourly = build_hourly_supertrend(spot, period=10, multiplier=3.0)
    flip_events = extract_flip_events(hourly, period=10, multiplier=3.0)
    print(f"1H bars: {len(hourly):,} | Flip events: {len(flip_events)}")

    expiry_cal = ExpiryCalendar()
    available_dates = loader.available_dates()
    print(f"Trading days available: {len(available_dates)}")

    print("\nRunning backtest: roll_forward=False (stay flat after expiry backstop)...")
    trades_no_roll = run_backtest(loader, expiry_cal, flip_events, available_dates, roll_forward=False)

    print("Running backtest: roll_forward=True (roll into next week, same direction)...")
    trades_roll = run_backtest(loader, expiry_cal, flip_events, available_dates, roll_forward=True)

    res_no_roll = summarize(trades_no_roll, "roll_forward=False")
    res_roll = summarize(trades_roll, "roll_forward=True")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    for res in (res_no_roll, res_roll):
        if not res:
            continue
        out_path = RESULTS_DIR / f"trades_{res['label']}.csv"
        res["df"].drop(columns=["running_peak"]).to_csv(out_path, index=False)
        print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
