# ============================================================
# backtesting/etf_strategies/red_week_dca_sweep.py
#
# Sweeps the DEPTH of the red week that triggers deployment.
#
# The plain rule ("any red week") fires on 49% of weeks, so the pot
# never grows and the result collapses onto a plain SIP. This asks the
# real question: does waiting for a GENUINE dip -- down 2%, 3%, 5% --
# finally beat buying every week?
#
# Rs5,000 still accrues every week, so the cashflow schedule is
# identical across every row and XIRR stays directly comparable.
#
#   python backtesting/etf_strategies/red_week_dca_sweep.py
#   python backtesting/etf_strategies/red_week_dca_sweep.py --symbol GOLDBEES
# ============================================================

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.append(str(Path(__file__).parent.parent.parent))
sys.path.append(str(Path(__file__).parent))

from etf.etf_loader import ETFLoader          # noqa: E402
from red_week_dca import (                    # noqa: E402
    CONTRIB, RESULTS, metrics, simulate, weekly_bars,
)

THRESHOLDS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]


def week_return(w: pd.DataFrame, defn: str) -> pd.Series:
    """Weekly return under each 'red week' definition."""
    if defn == "candle":
        return w["close"] / w["open"] - 1.0        # vs its own open
    return w["close"] / w["close"].shift(1) - 1.0  # vs last week's close


def wait_stats(trigger: pd.Series) -> tuple[float, int]:
    """Average and worst number of weeks the cash sits waiting."""
    gaps, run = [], 0
    for fired in trigger:
        run += 1
        if fired:
            gaps.append(run)
            run = 0
    if not gaps:
        return float("nan"), len(trigger)
    return float(np.mean(gaps)), int(max(gaps))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTYBEES")
    ap.add_argument("--start", default=None)
    ap.add_argument("--contrib", type=int, default=CONTRIB)
    args = ap.parse_args()

    daily = ETFLoader().load(args.symbol, start=args.start)
    w = weekly_bars(daily)

    print(f"{args.symbol}  |  {len(w)} weekly bars  |  "
          f"{w.index.min().date()} -> {w.index.max().date()}  |  "
          f"Rs{args.contrib:,}/week  |  total Rs{args.contrib * len(w):,}\n")

    rows = []

    # Benchmark first: buy every single week.
    always = pd.Series(True, index=w.index)
    for rate, rtag in [(0.0, "0%"), (0.06, "6%")]:
        m = metrics(simulate(w, always, args.contrib, rate),
                    f"SIP every week (cash {rtag})", args.contrib)
        m.update(defn="-", thr=None, avg_wait=1.0, max_wait=1)
        rows.append(m)

    for defn in ("candle", "wow"):
        ret = week_return(w, defn)
        for thr in THRESHOLDS:
            trigger = (ret <= -thr / 100.0).fillna(False)
            if trigger.sum() == 0:
                continue
            aw, mw = wait_stats(trigger)
            for rate, rtag in [(0.0, "0%"), (0.06, "6%")]:
                m = metrics(simulate(w, trigger, args.contrib, rate),
                            f"{defn} <= -{thr:g}% (cash {rtag})", args.contrib)
                m.update(defn=defn, thr=thr, avg_wait=round(aw, 1), max_wait=mw)
                rows.append(m)

    out = pd.DataFrame(rows)[[
        "variant", "buys", "avg_wait", "max_wait", "deployed", "idle_cash_end",
        "units", "avg_cost", "final_value", "xirr", "max_dd",
    ]]
    pd.set_option("display.width", 260)
    print(out.to_string(index=False))

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"red_week_sweep_{args.symbol}.csv"
    out.to_csv(path, index=False)

    sip = out[out["variant"].str.startswith("SIP")]["xirr"].max()
    rule = out[~out["variant"].str.startswith("SIP")]
    best = rule.loc[rule["xirr"].idxmax()]
    beat = rule[rule["xirr"] > sip]

    print(f"\nbest SIP XIRR          : {sip}%")
    print(f"best threshold variant : {best['variant']}  {best['xirr']}%  "
          f"({best['buys']} buys, avg wait {best['avg_wait']}w)")
    print(f"variants beating SIP   : {len(beat)} of {len(rule)}")
    if len(beat):
        print(beat[["variant", "buys", "avg_wait", "xirr", "max_dd"]].to_string(index=False))
    print(f"\nsaved -> {path}")


if __name__ == "__main__":
    main()
