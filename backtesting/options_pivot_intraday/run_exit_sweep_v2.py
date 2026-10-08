"""
Exit-side sweep on a chosen Supertrend setting (default ST(14,2.5)), to see
whether the exit improvements stack on top of the slower trend filter.

Usage:
    python run_exit_sweep_v2.py [period] [multiplier]

Account model: Rs 20L, 10 lots, Rs 20/order (Rs 40 per round trip).
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

ST_PERIOD = int(sys.argv[1]) if len(sys.argv) > 1 else 14
ST_MULT = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5

VARIANTS = [
    ("baseline (ST exit)",     dict()),
    ("confirm 2 bars",         dict(exit_confirm_bars=2)),
    ("confirm 3 bars",         dict(exit_confirm_bars=3)),
    ("confirm 4 bars",         dict(exit_confirm_bars=4)),
    ("ST exit + stop 1.5x",    dict(stop_mult=1.5)),
    ("ST exit + stop 2.0x",    dict(stop_mult=2.0)),
    ("ST exit + stop 2.5x",    dict(stop_mult=2.5)),
    ("ST exit + stop 3.0x",    dict(stop_mult=3.0)),
    ("confirm 2 + stop 2.0x",  dict(exit_confirm_bars=2, stop_mult=2.0)),
    ("confirm 3 + stop 2.0x",  dict(exit_confirm_bars=3, stop_mult=2.0)),
    ("no ST exit, stop 1.5x",  dict(use_st_exit=False, stop_mult=1.5)),
    ("no ST exit, stop 2.0x",  dict(use_st_exit=False, stop_mult=2.0)),
]


def summarize(trades, label=""):
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
        "variant": label, "trades": len(df),
        "win%": round((df.pnl_net > 0).mean() * 100, 1),
        "PF": round(wins / abs(loss), 2) if loss else None,
        "net_L": round(net / 100000, 2), "CAGR%": round(cagr, 2),
        "maxDD%": round(df.dd.min(), 2),
        "retDD": round(cagr / abs(df.dd.min()), 2) if df.dd.min() else None,
        "avg": round(df.pnl_net.mean()),
        "worst": round(df.pnl_net.min()),
    }


def main():
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    bars = add_supertrend_7_3(build_5min_bars(spot), period=ST_PERIOD, multiplier=ST_MULT)
    dcol, scol = f"supertrend_dir_{ST_PERIOD}_{ST_MULT}", f"supertrend_{ST_PERIOD}_{ST_MULT}"
    pivots = build_daily_pivots(spot)
    print(f"ST({ST_PERIOD},{ST_MULT}) | {len(bars):,} bars | {len(pivots):,} days\n")

    rows = []
    for label, kw in VARIANTS:
        s = summarize(run_backtest(loader, bars, pivots, dcol, scol, **kw), label)
        # IS / OOS stability for each variant
        is_ = summarize(run_backtest(loader, bars, pivots, dcol, scol,
                                     date_from="2021-01-01", date_to="2023-12-31", **kw))
        oos = summarize(run_backtest(loader, bars, pivots, dcol, scol,
                                     date_from="2024-01-01", date_to="2026-12-31", **kw))
        s["IS_L"], s["IS_PF"] = is_["net_L"], is_["PF"]
        s["OOS_L"], s["OOS_PF"] = oos["net_L"], oos["PF"]
        rows.append(s)
        print(f"  {label:22s} n={s['trades']:5d} net=Rs{s['net_L']:7.2f}L CAGR={s['CAGR%']:6.2f}% "
              f"DD={s['maxDD%']:7.2f}% PF={s['PF']} retDD={s['retDD']:5.2f} "
              f"IS={s['IS_L']:6.2f} OOS={s['OOS_L']:6.2f}")

    out = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(RESULTS_DIR / f"exit_sweep_ST{ST_PERIOD}_{ST_MULT}.csv", index=False)
    print("\n" + "=" * 132)
    print(f"EXIT SWEEP on ST({ST_PERIOD},{ST_MULT})  —  Rs 20L, 10 lots, Rs 20/order")
    print("=" * 132)
    print(out.to_string(index=False))
    print(f"\nSaved: {RESULTS_DIR / f'exit_sweep_ST{ST_PERIOD}_{ST_MULT}.csv'}")


if __name__ == "__main__":
    main()
