import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
xs_taker: long-only vs short-only vs long+short, $10,000, on Delta-tradable coins.

  long+short : $5k long top-20% + $5k short bottom-20%  (the version shown before)
  long-only  : full $10k long the top-20% book
  short-only : full $10k short the bottom-20% book
  benchmarks : $10k equally risk-weighted in ALL eligible coins (long), BTC buy & hold

Same rules otherwise: 14d taker-buy ratio, rebalance every 3 days, inverse-vol,
15 bps/side, funding. Periods: Delta 2024+ (main) and the whole PIT history 2020+.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.sim_xs_taker_sides
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import engine as E
from .plot_equity import SURFACE, INK, INK2, GRID, _style, _usd
from .robust_xs_taker import taker_ratio

ROOT = Path(__file__).parents[2]
OUT = Path(__file__).parent / "results"
CAPITAL = 10_000
# categorical slots 1-4 of the reference palette (validated adjacent order)
COLORS = {"long+short": "#2a78d6", "long-only": "#eb6834", "short-only": "#1baf7a",
          "hold all eligible": "#eda100", "BTC buy & hold": "#8a8984"}


def stats(r: pd.Series) -> dict:
    eq = (1 + r).cumprod() * CAPITAL
    yrs = len(r) / 365
    m = (1 + r).resample("ME").prod() - 1
    y = (1 + r).groupby(r.index.year).prod() - 1
    return {"end $": eq.iloc[-1], "CAGR": (eq.iloc[-1] / CAPITAL) ** (1 / yrs) - 1,
            "maxDD": (eq / eq.cummax() - 1).min(),
            "Sharpe": r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else np.nan,
            "worst month": m.min(), "% months up": (m > 0).mean(),
            **{str(k): v for k, v in y.items()}}


def main() -> None:
    liq = pd.read_csv(ROOT / "crypto" / "universe" / "delta_perps_liquidity.csv")
    delta = set(liq.loc[liq["turnover_24h_usd"] >= 50e3, "binance"].dropna())
    P = E.Panel("1d", universe="pit")
    btc_px = P.open["BTCUSDT"]

    curves = {}
    for label, allowed, start in (("Delta coins, 2024 →", delta, "2024-01-01"),
                                  ("all PIT top-100, 2020 →", set(P.close.columns), "2020-03-01")):
        tr = taker_ratio(P, 14)
        tr.loc[:, [c for c in tr.columns if c not in allowed]] = np.nan
        w = E.xs_weights(P, tr, 3)
        ones = pd.DataFrame(1.0, index=P.index, columns=P.close.columns).where(tr.notna(), 0.0)
        hold = E.ts_weights(P, ones)
        hold = hold.div(hold.sum(axis=1).replace(0, np.nan), axis=0).fillna(0.0)   # $10k, risk-weighted
        variants = {
            "long+short": 0.5 * w,
            "long-only": w.clip(lower=0),
            "short-only": w.clip(upper=0),
            "hold all eligible": hold,
        }
        rows, daily = {}, {}
        for name, wv in variants.items():
            r = E.run(P, wv, 15)["net"].resample("1D").sum().loc[start:]
            rows[name], daily[name] = stats(r), r
        b = btc_px.pct_change().shift(-1).fillna(0).resample("1D").sum().loc[start:]
        rows["BTC buy & hold"], daily["BTC buy & hold"] = stats(b), b
        t = pd.DataFrame(rows).T
        print(f"\n=== {label}  ($10,000 start, 15 bps/side + funding) ===")
        fmt = {c: (lambda v: f"${v:,.0f}") if c == "end $" else
               (lambda v: f"{v:.2f}") if c == "Sharpe" else (lambda v: f"{v:.0%}") for c in t.columns}
        print(t.to_string(formatters=fmt))
        t.to_csv(OUT / f"xs_taker_sides_{'delta' if 'Delta' in label else 'pit'}.csv")
        curves[label] = daily

    fig, axes = plt.subplots(1, 2, figsize=(15, 6.4), facecolor=SURFACE)
    for ax, (label, daily) in zip(axes, curves.items()):
        _style(ax)
        ax.xaxis.set_major_locator(mdates.YearLocator(1 if "2024" in label else 1))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        for name, r in daily.items():
            eq = (1 + r).cumprod() * CAPITAL
            ax.plot(eq.index, eq.values, color=COLORS[name], lw=2 if name == "long+short" else 1.5,
                    label=f"{name}  ${eq.iloc[-1]:,.0f}")
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(_usd)
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1, 2, 5)))
        ax.axhline(CAPITAL, color=INK2, lw=0.8)
        ax.set_title(f"xs_taker — {label}", loc="left", fontsize=11.5, color=INK, fontweight="bold")
        ax.legend(loc="upper left", bbox_to_anchor=(0, -0.08), ncol=3, frameon=False, fontsize=9,
                  labelcolor=INK2)
    fig.suptitle("Long-only vs short-only vs long+short, $10,000 start, 15 bps/side + funding",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout()
    out = OUT / "equity_xs_taker_sides.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print("\n" + str(out))


if __name__ == "__main__":
    main()
