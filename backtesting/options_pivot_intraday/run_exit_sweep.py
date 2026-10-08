"""
Exit-side sweep + in-sample / out-of-sample check for the default version.

Exit levers tested:
  exit_confirm_bars : require N consecutive adverse bars before stopping out
  use_st_exit=False : drop the Supertrend stop entirely (hold to 15:15)
  stop_mult         : premium stop -- exit if the option trades at
                      entry_price x stop_mult (checked on 1-min highs, so an
                      intrabar spike triggers it like a real stop order)

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
ST_PERIOD, ST_MULT = 7, 3.0
CAPITAL, LOTS, BRK = 2_000_000, 10, 40

EXIT_VARIANTS = [
    ("baseline (ST exit)",      dict()),
    ("confirm 2 bars",          dict(exit_confirm_bars=2)),
    ("confirm 3 bars",          dict(exit_confirm_bars=3)),
    ("confirm 4 bars",          dict(exit_confirm_bars=4)),
    ("no ST exit, stop 1.5x",   dict(use_st_exit=False, stop_mult=1.5)),
    ("no ST exit, stop 2.0x",   dict(use_st_exit=False, stop_mult=2.0)),
    ("no ST exit, stop 2.5x",   dict(use_st_exit=False, stop_mult=2.5)),
    ("no ST exit, stop 3.0x",   dict(use_st_exit=False, stop_mult=3.0)),
    ("no ST exit, NO stop",     dict(use_st_exit=False)),
    ("ST exit + stop 2.0x",     dict(stop_mult=2.0)),
    ("confirm 2 + stop 2.0x",   dict(exit_confirm_bars=2, stop_mult=2.0)),
]


def stats(trades, label):
    if not trades:
        return {"variant": label, "trades": 0}
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    df["pnl_net"] = df["pnl_rupees"] * LOTS - BRK
    df["equity"] = CAPITAL + df["pnl_net"].cumsum()
    df["dd"] = (df["equity"] - df["equity"].cummax()) / df["equity"].cummax() * 100
    net = df["pnl_net"].sum()
    yrs = (df["exit_time"].max() - df["exit_time"].min()).days / 365.25
    cagr = ((CAPITAL + net) / CAPITAL) ** (1 / yrs) - 1
    wins = df.loc[df.pnl_net > 0, "pnl_net"].sum()
    loss = df.loc[df.pnl_net < 0, "pnl_net"].sum()
    return {
        "variant": label, "trades": len(df),
        "win%": round((df.pnl_net > 0).mean() * 100, 1),
        "PF": round(wins / abs(loss), 2) if loss else None,
        "net_L": round(net / 100000, 2), "CAGR%": round(cagr * 100, 2),
        "maxDD%": round(df.dd.min(), 2),
        "avg": round(df.pnl_net.mean()),
        "ret/DD": round(cagr * 100 / abs(df.dd.min()), 2) if df.dd.min() else None,
        "worst": round(df.pnl_net.min()),
    }


def main():
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    bars = add_supertrend_7_3(build_5min_bars(spot), period=ST_PERIOD, multiplier=ST_MULT)
    dir_col, st_col = f"supertrend_dir_{ST_PERIOD}_{ST_MULT}", f"supertrend_{ST_PERIOD}_{ST_MULT}"
    pivots = build_daily_pivots(spot)
    print(f"{len(bars):,} 5-min bars | {len(pivots):,} tradeable days\n")

    rows = []
    for label, kw in EXIT_VARIANTS:
        s = stats(run_backtest(loader, bars, pivots, dir_col, st_col, **kw), label)
        rows.append(s)
        print(f"  {label:24s} n={s['trades']:5d}  net=Rs{s['net_L']:7.2f}L  CAGR={s['CAGR%']:6.2f}%  "
              f"DD={s['maxDD%']:7.2f}%  PF={s['PF']}  worst=Rs{s['worst']:,}")

    out = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(RESULTS_DIR / "exit_sweep.csv", index=False)
    print("\n" + "=" * 120)
    print("EXIT-SIDE SWEEP  —  Rs 20L, 10 lots, Rs 20/order")
    print("=" * 120)
    print(out.to_string(index=False))

    # ── in-sample / out-of-sample on the DEFAULT version ──────────────────
    print("\n" + "=" * 120)
    print("IN-SAMPLE / OUT-OF-SAMPLE  —  default version (no filters)")
    print("=" * 120)
    splits = [
        ("IS  2021-2023", "2021-01-01", "2023-12-31"),
        ("OOS 2024-2026", "2024-01-01", "2026-12-31"),
        ("FULL 2021-2026", None, None),
    ]
    rows2 = []
    for label, d0, d1 in splits:
        t = run_backtest(loader, bars, pivots, dir_col, st_col, date_from=d0, date_to=d1)
        s = stats(t, label)
        df = pd.DataFrame(t)
        df["pnl_net"] = df["pnl_rupees"] * LOTS - BRK
        s["days"] = df["date"].nunique()
        s["yrs"] = round((pd.to_datetime(df["exit_time"]).max() - pd.to_datetime(df["exit_time"]).min()).days / 365.25, 2)
        rows2.append(s)
    oos = pd.DataFrame(rows2)
    oos.to_csv(RESULTS_DIR / "is_oos.csv", index=False)
    print(oos.to_string(index=False))
    print(f"\nSaved: {RESULTS_DIR/'exit_sweep.csv'}  and  {RESULTS_DIR/'is_oos.csv'}")


if __name__ == "__main__":
    main()
