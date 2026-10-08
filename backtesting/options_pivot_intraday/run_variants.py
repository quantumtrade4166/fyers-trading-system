"""
Variant sweep for the intraday pivot + Supertrend strategy.

Tests anti-whipsaw entry filters against the unmodified baseline:
  - cooldown_bars : block re-entry for N bars after an exit
  - fresh_cross   : only enter on a genuine false->true crossover
  - st_buffer     : require close to clear the Supertrend line by N points

All results are reported on the Rs 20L / 10-lot account model with
Rs 20/order brokerage (Rs 40 per round-trip trade, flat regardless of lots).
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

VARIANTS = [
    ("baseline",              dict()),
    ("cooldown 3",            dict(cooldown_bars=3)),
    ("cooldown 6",            dict(cooldown_bars=6)),
    ("cooldown 9",            dict(cooldown_bars=9)),
    ("cooldown 12",           dict(cooldown_bars=12)),
    ("fresh cross",           dict(fresh_cross=True)),
    ("fresh cross + cd3",     dict(fresh_cross=True, cooldown_bars=3)),
    ("fresh cross + cd6",     dict(fresh_cross=True, cooldown_bars=6)),
    ("buffer 15",             dict(st_buffer=15)),
    ("buffer 25",             dict(st_buffer=25)),
    ("buffer 40",             dict(st_buffer=40)),
    ("buffer 60",             dict(st_buffer=60)),
    ("buffer 25 + cd3",       dict(st_buffer=25, cooldown_bars=3)),
    ("buffer 25 + cd6",       dict(st_buffer=25, cooldown_bars=6)),
    ("buffer 40 + cd6",       dict(st_buffer=40, cooldown_bars=6)),
]


def stats(trades, label):
    if not trades:
        return {"variant": label, "trades": 0}
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    df["pnl_net"] = df["pnl_rupees"] * LOTS - BRK
    df["equity"] = CAPITAL + df["pnl_net"].cumsum()
    df["dd"] = (df["equity"] - df["equity"].cummax()) / df["equity"].cummax() * 100
    df["hold_min"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 60

    net = df["pnl_net"].sum()
    final = CAPITAL + net
    yrs = (df["exit_time"].max() - df["exit_time"].min()).days / 365.25
    cagr = ((final / CAPITAL) ** (1 / yrs) - 1) * 100
    wins = df.loc[df.pnl_net > 0, "pnl_net"].sum()
    loss = df.loc[df.pnl_net < 0, "pnl_net"].sum()
    churn = df[df["hold_min"] <= 30]
    return {
        "variant": label, "trades": len(df),
        "win%": round((df.pnl_net > 0).mean() * 100, 1),
        "PF": round(wins / abs(loss), 2) if loss else None,
        "net_L": round(net / 100000, 2), "CAGR%": round(cagr, 2),
        "maxDD%": round(df.dd.min(), 2),
        "avg": round(df.pnl_net.mean()),
        "ret/DD": round(cagr / abs(df.dd.min()), 2) if df.dd.min() else None,
        "churn_n": len(churn), "churn_L": round(churn.pnl_net.sum() / 100000, 2),
    }


def main():
    print("Loading data / building signals...")
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    bars = add_supertrend_7_3(build_5min_bars(spot), period=ST_PERIOD, multiplier=ST_MULT)
    dir_col = f"supertrend_dir_{ST_PERIOD}_{ST_MULT}"
    st_col = f"supertrend_{ST_PERIOD}_{ST_MULT}"
    pivots = build_daily_pivots(spot)
    print(f"{len(bars):,} 5-min bars | {len(pivots):,} tradeable days\n")

    rows = []
    for label, kw in VARIANTS:
        trades = run_backtest(loader, bars, pivots, dir_col, st_col, **kw)
        s = stats(trades, label)
        rows.append(s)
        print(f"  {label:22s} trades={s['trades']:5d}  net=Rs{s['net_L']:7.2f}L  "
              f"CAGR={s['CAGR%']:6.2f}%  DD={s['maxDD%']:6.2f}%  PF={s['PF']}")
        if label == "baseline":
            pd.DataFrame(trades).to_csv(RESULTS_DIR / "trades.csv", index=False)

    out = pd.DataFrame(rows)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.to_csv(RESULTS_DIR / "variant_sweep.csv", index=False)
    print("\n" + "=" * 118)
    print("VARIANT SWEEP  —  Rs 20L capital, 10 lots, Rs 20/order")
    print("=" * 118)
    print(out.to_string(index=False))
    print(f"\nSaved: {RESULTS_DIR / 'variant_sweep.csv'}")


if __name__ == "__main__":
    main()
