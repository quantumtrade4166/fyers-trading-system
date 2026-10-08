"""
is_oos_clean.py
In-Sample / Out-of-Sample validation on CLEAN data.

Replaces `is_oos_test.py`, whose result was withdrawn because it ran on
unadjusted prices with the broken yfinance index, AND because its verdict logic
was wrong: the ACCEPTABLE branch tested only the CAGR gap and ignored the Sharpe
gap entirely, so a 5.6x Sharpe difference passed unnoticed. It printed
ACCEPTABLE while the vault recorded "ROBUST".

Here both gaps must pass, at every tier.

Design
  IS   trade 2007-01 -> 2015-12   (the years available while the rules were chosen)
  OOS  trade 2016-01 -> 2026-09   (held out)
Each run starts fresh at Rs 10L. Data is loaded from 2005 so BOTH windows get a
full 252-day lookback rather than the OOS window losing its first year.

The rules are identical in both windows -- nothing is refitted. This asks a
single question: does the edge exist in a decade the design never saw?
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from dualmom_final import backtest, load_nifty, RESULTS, STOP_LOSS

DATA_START = "2005-01-01"
IS_START, IS_END = "2007-01-01", "2015-12-31"
OOS_START, OOS_END = "2016-01-01", "2026-09-30"


def nifty_cagr(a, b):
    n, _ = load_nifty()
    seg = n.loc[a:b]
    yrs = (seg.index[-1] - seg.index[0]).days / 365.25
    return (seg.iloc[-1] / seg.iloc[0]) ** (1 / yrs) - 1


def main():
    print(f"DualMom.Liq.Nifty50 — IS/OOS on clean data   (stop-loss {STOP_LOSS})\n")
    runs = {}
    for lbl, a, b in (("In-Sample 2007-2015", IS_START, IS_END),
                      ("Out-of-Sample 2016-2026", OOS_START, OOS_END),
                      ("Full 2007-2026", IS_START, OOS_END)):
        r = backtest(40, 1_000_000, True, STOP_LOSS, start=a, end=b, data_start=DATA_START)
        r["nifty"] = nifty_cagr(r["nav"].index[0], r["nav"].index[-1])
        runs[lbl] = r
        print(f"  {lbl:<26} done  ({r['years']:.1f} yrs)")

    print()
    print("=" * 92)
    print(f"{'Metric':<24}{'In-Sample':>20}{'Out-of-Sample':>20}{'Full':>20}")
    print("=" * 92)
    keys = list(runs)
    rows = [
        ("CAGR",              lambda r: f"{r['cagr']*100:.2f}%"),
        ("Sharpe",            lambda r: f"{r['sharpe']:.3f}"),
        ("Sortino",           lambda r: f"{r['sortino']:.3f}"),
        ("Max DD (daily)",    lambda r: f"{r['dd_daily']*100:.1f}%"),
        ("Winning months",    lambda r: f"{r['win_rate']:.1f}%"),
        ("Months IN / OUT",   lambda r: f"{r['months_in']}/{r['months_out']}"),
        ("Stop-loss firings", lambda r: f"{r['n_stops']}"),
        ("Final NAV",         lambda r: f"Rs {r['final']/1e7:.2f} Cr"),
        ("Nifty 50 CAGR",     lambda r: f"{r['nifty']*100:.2f}%"),
        ("Alpha vs Nifty",    lambda r: f"{(r['cagr']-r['nifty'])*100:+.2f} pts"),
    ]
    for name, fn in rows:
        print(f"{name:<24}" + "".join(f"{fn(runs[k]):>20}" for k in keys))
    print("=" * 92)

    is_r, oos_r = runs[keys[0]], runs[keys[1]]
    cg = abs(is_r["cagr"] - oos_r["cagr"]) * 100
    sg = abs(is_r["sharpe"] - oos_r["sharpe"])
    print(f"\nIS vs OOS gaps:   CAGR {cg:.2f} pts   |   Sharpe {sg:.3f}")
    print("(the old script only checked the CAGR gap, which is how a 1.633 Sharpe"
          "\n gap was labelled ROBUST -- both must pass here)\n")

    if cg < 5 and sg < 0.30:
        v = "ROBUST — IS and OOS are closely aligned. No sign of overfitting."
    elif cg < 10 and sg < 0.60:
        v = "ACCEPTABLE — moderate drift between periods. Normal for momentum."
    else:
        v = "CAUTION — large IS/OOS gap. The edge is not stable across regimes."
    print(f"  VERDICT: {v}")

    if oos_r["cagr"] > is_r["cagr"]:
        print("\n  Note: OOS BEATS IS. That is not proof of robustness on its own —"
              "\n  2016-2026 was a far kinder market than 2007-2015 (which contains the"
              "\n  GFC). Compare each period's alpha over Nifty, not its raw CAGR.")

    RESULTS.mkdir(exist_ok=True)
    pd.DataFrame({k: {n: f(runs[k]) for n, f in rows} for k in keys}).to_csv(
        RESULTS / "is_oos_clean.csv")
    for k, r in runs.items():
        r["nav"].to_csv(RESULTS / f"is_oos_{k.split()[0].lower().replace('-','')}_nav.csv",
                        header=["nav"])
    print(f"\nsaved -> {RESULTS}")


if __name__ == "__main__":
    main()
