import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC target feasibility — can any basket reach CAGR 30-40% with MaxDD <= 10%?
==============================================================================
Target restated: CAGR/MaxDD >= 3.0. Sizing moves CAGR and DD together, so
this scales each basket's risk until MaxDD hits exactly 10% and reports the
CAGR it earns there (daily compounding, leverage allowed — Delta perp).

Baskets (daily P&L of each combo at 1% risk on $10k, from the full history):
  all_4h+     every screened combo at 4h/8h/12h/1D, equal weight   (no selection)
  all_4h+_iv  same, inverse-volatility weighted (trailing 90d, lagged 1 day)
  robust5     S052 8h+12h, S118 12h, S023 12h, S024 4h — ⚠️ chosen using
              2022-26 robustness results, so its OOS number is optimistic
  S052_8h     best single

Each measured on FULL (2018-2026) and OOS (2022-2026). Leverage k is fitted on
the window it is reported on, which flatters it — the "k from IS" column shows
what happens when the size is fixed on 2018-21 and then traded 2022-26.

Run: .venv\\Scripts\\python.exe backtesting\\book_strategies\\kaufman\\run_btc_target.py
"""

import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.run_btc_robustness import trades_for
from backtesting.book_strategies.kaufman.run_btc_screen import CAPITAL, SPLIT, RESULTS_DIR
from backtesting.book_strategies.kaufman.data_btc import get_bars


def mtm_daily(trades, day_close: pd.Series) -> pd.Series:
    """Mark-to-market daily P&L. Exit-day booking hides every drawdown that
    happens while a trade is open (trend trades hold for weeks) — Sharpe and
    MaxDD on that series are fiction, and any weighting built on its
    volatility is worse. Each open position is marked at the 00:00 UTC daily
    close; costs and funding land on the exit day."""
    out = pd.Series(0.0, index=day_close.index)
    for t in trades:
        sgn = 1.0 if t.direction == "long" else -1.0
        d0 = pd.Timestamp(t.entry_time).normalize()
        d1 = pd.Timestamp(t.exit_time).normalize()
        marks = day_close.loc[d0:d1].copy()
        if marks.empty:
            continue
        marks.iloc[-1] = t.exit_price
        prev = marks.shift(1)
        prev.iloc[0] = t.entry_price
        out.loc[marks.index] += sgn * (marks - prev).to_numpy() * t.units
        out.loc[d1] -= t.cost
    return out


def mtm_job(name, tf, mode):
    try:
        tr = trades_for(get_bars(tf), name, mode)
    except Exception:
        return None
    if not tr:
        return None
    return f"{name}|{tf}|{mode}", mtm_daily(tr, get_bars("1D")["close"])

TARGET_DD = 10.0
ROBUST5 = ["S052_MACD|8h|ls", "S052_MACD|12h|ls", "S118_ATRVolBrk|12h|ls",
           "S023_KeltnerBreak|12h|ls", "S024_BBBreak|4h|ls"]


def curve(r: pd.Series, k: float):
    eq = (1 + k * r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100 if eq.iloc[-1] > 0 else -100
    dd = ((eq / eq.cummax()) - 1).min() * 100
    return cagr, dd


def k_for_dd(r: pd.Series, target=TARGET_DD) -> float:
    lo, hi = 0.0, 200.0
    for _ in range(60):
        mid = (lo + hi) / 2
        _, dd = curve(r, mid)
        if -dd > target:
            hi = mid
        else:
            lo = mid
    return lo


def main():
    screen = pd.read_csv(sorted(RESULTS_DIR.glob("btc_screen_*.csv"))[-1])
    combos = [(r.strategy, r.timeframe, r["mode"]) for _, r in
              screen[screen.timeframe.isin(["4h", "8h", "12h", "1D"])].iterrows()]
    with ProcessPoolExecutor(max_workers=6) as ex:
        daily = [x for x in ex.map(mtm_job, *zip(*combos)) if x]
    pnl = pd.concat({k: s for k, s in daily}, axis=1).fillna(0.0)
    pnl = pnl[(pnl.index >= "2018-01-01")]
    ret = pnl / CAPITAL

    # Inverse vol on a 365-day window, lagged; vol floored at the cross-
    # sectional median so a combo that barely traded can't grab the weight.
    vol = ret.rolling(365, min_periods=180).std().shift(1)
    vol = vol.clip(lower=vol.median(axis=1), axis=0)
    w = (1 / vol)
    w = w.div(w.sum(axis=1), axis=0).fillna(0)

    baskets = {
        "all_4h+ (equal)": ret.mean(axis=1),
        "all_4h+ (inv-vol)": (ret * w).sum(axis=1),
        "robust5 (biased)": ret[ROBUST5].mean(axis=1),
        "S052_MACD 8h": ret["S052_MACD|8h|ls"],
    }

    print("=" * 112)
    print(f"  TARGET: CAGR 30-40% with MaxDD <= {TARGET_DD:.0f}%  (needs CAGR/DD >= 3)")
    print("  Each basket levered until MaxDD = 10% on the window shown")
    print("=" * 112)
    print(f"  {'basket':<20}{'window':<8}{'Sharpe':>7}{'lev k':>7}{'CAGR@DD10':>11}"
          f"{'ratio':>7}{'   k from IS -> OOS CAGR / DD':>32}")
    for name, r in baskets.items():
        k_is = k_for_dd(r[r.index < SPLIT])
        for label, part in (("FULL", r), ("OOS", r[r.index >= SPLIT])):
            sh = part.mean() / part.std() * np.sqrt(365)
            k = k_for_dd(part)
            cagr, dd = curve(part, k)
            extra = ""
            if label == "OOS":
                c2, d2 = curve(part, k_is)
                extra = f"k={k_is:5.1f} -> {c2:6.1f}% / {d2:6.1f}%"
            print(f"  {name:<20}{label:<8}{sh:>7.2f}{k:>7.1f}{cagr:>10.1f}%{cagr/TARGET_DD:>7.2f}   {extra}")
    print("\n  Sharpe needed for 35% CAGR at 10% DD over ~5 years is roughly 3+.")


if __name__ == "__main__":
    main()
