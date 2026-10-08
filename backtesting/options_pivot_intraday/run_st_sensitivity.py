"""
Supertrend parameter sensitivity for the intraday pivot strategy.

Supertrend(7, 3) was a free choice, so this checks whether it sits on a broad
plateau of working settings (robust) or is an isolated lucky peak (fragile).

Reports full-period plus IS/OOS net for each combo, on the Rs 20L / 10-lot
model with Rs 20/order. Note: each split restarts at Rs 20L, so IS/OOS
percentages are not comparable to the full-period figure.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from backtesting.options_credit_spread.data_loader import OptionsDataLoader
from backtesting.options_pivot_intraday.signals import (
    build_5min_bars, add_supertrend_7_3, build_daily_pivots,
)
from backtesting.options_pivot_intraday.strategy import run_backtest

RESULTS_DIR = Path(__file__).resolve().parent / "results"
CAPITAL, LOTS, BRK = 2_000_000, 10, 40

PERIODS = [5, 7, 10, 14]
MULTS = [2.0, 2.5, 3.0, 3.5]


def summarize(trades):
    if not trades:
        return None
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    df["pnl_net"] = df["pnl_rupees"] * LOTS - BRK
    df["eq"] = CAPITAL + df["pnl_net"].cumsum()
    df["dd"] = (df["eq"] - df["eq"].cummax()) / df["eq"].cummax() * 100
    net = df["pnl_net"].sum()
    yrs = (df["exit_time"].max() - df["exit_time"].min()).days / 365.25
    cagr = (((CAPITAL + net) / CAPITAL) ** (1 / yrs) - 1) * 100
    wins = df.loc[df.pnl_net > 0, "pnl_net"].sum()
    loss = df.loc[df.pnl_net < 0, "pnl_net"].sum()
    return {
        "trades": len(df), "win": round((df.pnl_net > 0).mean() * 100, 1),
        "PF": round(wins / abs(loss), 2) if loss else None,
        "net_L": round(net / 100000, 2), "CAGR": round(cagr, 2),
        "DD": round(df.dd.min(), 2),
        "retDD": round(cagr / abs(df.dd.min()), 2) if df.dd.min() else None,
    }


def main():
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    base_bars = build_5min_bars(spot)
    pivots = build_daily_pivots(spot)
    print(f"{len(base_bars):,} 5-min bars | {len(pivots):,} tradeable days\n")

    rows = []
    for p in PERIODS:
        for m in MULTS:
            bars = add_supertrend_7_3(base_bars.copy(), period=p, multiplier=m)
            dcol, scol = f"supertrend_dir_{p}_{m}", f"supertrend_{p}_{m}"
            full = summarize(run_backtest(loader, bars, pivots, dcol, scol))
            is_ = summarize(run_backtest(loader, bars, pivots, dcol, scol,
                                         date_from="2021-01-01", date_to="2023-12-31"))
            oos = summarize(run_backtest(loader, bars, pivots, dcol, scol,
                                         date_from="2024-01-01", date_to="2026-12-31"))
            row = {"period": p, "mult": m, **full,
                   "IS_net_L": is_["net_L"] if is_ else None,
                   "IS_PF": is_["PF"] if is_ else None,
                   "OOS_net_L": oos["net_L"] if oos else None,
                   "OOS_PF": oos["PF"] if oos else None}
            rows.append(row)
            star = "  <-- current" if (p == 7 and m == 3.0) else ""
            print(f"  ST({p:2d},{m:.1f})  n={full['trades']:5d}  net=Rs{full['net_L']:7.2f}L  "
                  f"CAGR={full['CAGR']:6.2f}%  DD={full['DD']:7.2f}%  PF={full['PF']}  "
                  f"IS={is_['net_L']:6.2f}L OOS={oos['net_L']:6.2f}L{star}")

    out = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(RESULTS_DIR / "st_sensitivity.csv", index=False)
    print("\n" + "=" * 130)
    print("SUPERTREND SENSITIVITY  —  Rs 20L, 10 lots, Rs 20/order")
    print("=" * 130)
    print(out.to_string(index=False))

    print("\nNet profit (Rs L) grid — rows=period, cols=multiplier")
    print(out.pivot(index="period", columns="mult", values="net_L").to_string())
    print("\nProfit factor grid")
    print(out.pivot(index="period", columns="mult", values="PF").to_string())
    print("\nMax drawdown % grid")
    print(out.pivot(index="period", columns="mult", values="DD").to_string())
    print(f"\nSaved: {RESULTS_DIR / 'st_sensitivity.csv'}")


if __name__ == "__main__":
    main()
