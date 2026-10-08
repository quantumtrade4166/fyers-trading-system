"""
Minimum-premium filter test on ST(14, 2.5) + 2x premium stop.

Skip an entry signal when the ATM premium is below a floor, either a fixed
rupee amount or a percentage of Nifty spot (fairer across years, since
premiums scale with the index level). The filter is applied inside the
engine, so a skipped signal frees its entry slot for a later one that day.

Each day is traded independently (intraday, no carry-over), so IS/OOS are
taken as date slices of the full run. All results use the realistic cost
model (all charges + size-scaled slippage) from run_costs.py.
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
from backtesting.options_pivot_intraday.run_costs import simulate, stats, SCENARIOS, REALISTIC

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ST_PERIOD, ST_MULT = 14, 2.5
BASE_KW = dict(stop_mult=2.0)

FILTERS = [
    ("no filter",        dict()),
    ("prem >= 0.15%",    dict(min_premium_pct=0.15)),
    ("prem >= 0.20%",    dict(min_premium_pct=0.20)),
    ("prem >= 0.25%",    dict(min_premium_pct=0.25)),
    ("prem >= 0.30%",    dict(min_premium_pct=0.30)),
    ("prem >= 0.40%",    dict(min_premium_pct=0.40)),
    ("prem >= Rs 30",    dict(min_premium=30)),
    ("prem >= Rs 40",    dict(min_premium=40)),
    ("prem >= Rs 50",    dict(min_premium=50)),
    ("prem >= Rs 60",    dict(min_premium=60)),
    ("prem >= Rs 80",    dict(min_premium=80)),
]


def main():
    slip_fn = {l: f for l, _, f in SCENARIOS}[REALISTIC]
    loader = OptionsDataLoader()
    spot = loader.spot_1min_series()
    bars = add_supertrend_7_3(build_5min_bars(spot), period=ST_PERIOD, multiplier=ST_MULT)
    dcol, scol = f"supertrend_dir_{ST_PERIOD}_{ST_MULT}", f"supertrend_{ST_PERIOD}_{ST_MULT}"
    pivots = build_daily_pivots(spot)
    print(f"ST({ST_PERIOD},{ST_MULT}) + 2x stop | realistic costs | {len(pivots):,} days\n")

    rows = []
    for label, kw in FILTERS:
        t = pd.DataFrame(run_backtest(loader, bars, pivots, dcol, scol, **BASE_KW, **kw))
        t = t.sort_values("exit_time").reset_index(drop=True)
        t["date"] = t["date"].astype(str)
        is_t = t[t["date"] <= "2023-12-31"]
        oos_t = t[t["date"] >= "2024-01-01"]

        fx = stats(simulate(t, False, True, slip_fn))
        cp = stats(simulate(t, True, True, slip_fn))
        fis = stats(simulate(is_t, False, True, slip_fn))
        foos = stats(simulate(oos_t, False, True, slip_fn))
        y23 = t[(t["date"] >= "2023-01-01") & (t["date"] <= "2023-12-31")]
        f23 = simulate(y23, False, True, slip_fn)["net"].sum()

        row = {"filter": label, "trades": fx["trades"], "win%": fx["win%"], "PF": fx["PF"],
               "net_L": fx["net_L"], "CAGR%": fx["CAGR%"], "maxDD%": fx["maxDD%"],
               "cmp_net_L": cp["net_L"], "cmp_CAGR%": cp["CAGR%"], "cmp_DD%": cp["maxDD%"],
               "IS_PF": fis["PF"], "IS_net_L": fis["net_L"],
               "OOS_PF": foos["PF"], "OOS_net_L": foos["net_L"],
               "2023_net_L": round(f23 / 1e5, 2)}
        rows.append(row)
        print(f"  {label:15s} n={row['trades']:5d} PF={row['PF']}  net=Rs{row['net_L']:6.2f}L  "
              f"CAGR={row['CAGR%']:6.2f}%  DD={row['maxDD%']:7.2f}%  | IS PF {row['IS_PF']}  "
              f"OOS PF {row['OOS_PF']} | 2023 Rs{row['2023_net_L']:.2f}L")

    out = pd.DataFrame(rows)
    out.to_csv(RESULTS_DIR / "premium_filter.csv", index=False)
    print("\n" + "=" * 150)
    print("MINIMUM-PREMIUM FILTER - ST(14,2.5)+2x stop, realistic costs. "
          "Left block: fixed 10 lots. cmp_*: compounded. IS/OOS/2023: fixed 10 lots")
    print("=" * 150)
    print(out.to_string(index=False))
    print(f"\nSaved: {RESULTS_DIR / 'premium_filter.csv'}")


if __name__ == "__main__":
    main()
