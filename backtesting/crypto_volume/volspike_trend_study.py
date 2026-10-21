import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Volume spike + EMA 9/21 — improved: LONG ONLY + daily trend filter.

Fixed a priori (the owner's rule, not tuned): volume > 5x the 3-day average of 1h
candles, green spike candle, EMA9 crosses above EMA21 (same candle, or within 6),
stop = spike candle low, exit on the opposite EMA cross. Costs as volspike_ema.py.

Compared per coin, all from the same start (first day a 200-day EMA exists for BTC):
  A  both sides, no filter         (the original)
  B  long only, no filter
  C  long only + price above 200-day EMA     <- the proposal
  D  long only + price above 100-day EMA     (robustness: the filter length)
Portfolio: $2,500 in each of BTC/ETH/BNB/SOL (daily curves summed).

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.volspike_trend_study
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import volspike_ema as V
from .plot_equity import SURFACE, INK, INK2, _style, _usd

OUT = Path(__file__).parent / "results"
COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
START = pd.Timestamp("2020-07-19")
VARIANTS = {
    "A both sides, no filter": dict(),
    "B long only": dict(long_only=True),
    "C long + above 200d EMA": dict(long_only=True, trend_days=200),
    "D long + above 100d EMA": dict(long_only=True, trend_days=100),
}
COLORS = {"A both sides, no filter": "#8a8984", "B long only": "#eda100",
          "C long + above 200d EMA": "#2a78d6", "D long + above 100d EMA": "#1baf7a"}


def run(df, window, kw):
    sig = V.signals(df, days=3, mult=5.0, window=window, **kw)
    sig.loc[sig.index < START, ["long", "short"]] = False
    tr, cv = V.backtest(df, sig, "cross")
    return tr, cv.loc[START:]


def main() -> None:
    data = {c: V.load(c) for c in COINS}
    rows, curves, trades = [], {}, {}
    for window in (0, 6):
        print(f"\n=== window {window} (cross {'on the spike candle' if window == 0 else 'within 6 candles'}) ===")
        print(f"{'coin':<9}{'variant':<26}{'trades':>7}{'win%':>6}{'avgR':>7}{'PF':>6}{'end $':>9}"
              f"{'CAGR':>8}{'maxDD':>8}{'Sharpe':>7}{'yrs up':>8}")
        for coin, df in data.items():
            for name, kw in VARIANTS.items():
                tr, cv = run(df, window, kw)
                m = V.report(tr, cv)
                d = cv.resample("1D").last()
                yr = d.groupby(d.index.year).last() / d.groupby(d.index.year).first() - 1
                m["years_up"] = f"{int((yr > 0).sum())}/{len(yr)}"
                m["yearly"] = yr.round(4).to_dict()
                rows.append({"window": window, "coin": coin, "variant": name, **m})
                curves[(window, coin, name)] = d
                trades[(window, coin, name)] = tr
                print(f"{coin[:-4]:<9}{name:<26}{m['trades']:>7}{m['win%']:>6.0%}{m['avgR']:>+7.2f}"
                      f"{m['PF']:>6.2f}{m['end$']:>9,.0f}{m['CAGR']:>+8.1%}{m['maxDD']:>8.1%}"
                      f"{m['Sharpe']:>7.2f}{m['years_up']:>8}", flush=True)
        # 4-coin portfolio, $2,500 each
        print(f"{'4-coin portfolio ($2,500 each)':<35}")
        for name in VARIANTS:
            pf = sum(curves[(window, c, name)] / 4 for c in COINS).dropna()
            r = pf.pct_change().dropna()
            yrs = (pf.index[-1] - pf.index[0]).days / 365
            yr = pf.groupby(pf.index.year).last() / pf.groupby(pf.index.year).first() - 1
            n = sum(len(trades[(window, c, name)]) for c in COINS)
            print(f"{'  ' + name:<35}{n:>7} trades  end ${pf.iloc[-1]:>8,.0f}  CAGR {(pf.iloc[-1] / 1e4) ** (1 / yrs) - 1:+6.1%}"
                  f"  maxDD {(pf / pf.cummax() - 1).min():6.1%}  Sharpe {r.mean() / r.std() * np.sqrt(365):5.2f}"
                  f"  years: " + " ".join(f"{y}:{v:+.0%}" for y, v in yr.items()), flush=True)
            curves[(window, "PORTFOLIO", name)] = pf
    OUT.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "volspike_trend_study.csv", index=False)

    # chart: window 6, per coin + portfolio
    for window in (0, 6):
        fig, axes = plt.subplots(2, 3, figsize=(16, 8.5), facecolor=SURFACE)
        for ax, coin in zip(axes.flat, COINS + ["PORTFOLIO"]):
            _style(ax)
            ax.xaxis.set_major_locator(mdates.YearLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%y"))
            for name in VARIANTS:
                c = curves[(window, coin, name)]
                ax.plot(c.index, c.values, color=COLORS[name], lw=2 if name.startswith("C") else 1.3,
                        label=f"{name}  ${c.iloc[-1]:,.0f}")
            ax.axhline(10_000, color=INK2, lw=0.7)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.1f}k"))
            ax.set_title("4-coin portfolio ($2.5k each)" if coin == "PORTFOLIO" else coin[:-4],
                         loc="left", fontsize=11, color=INK, fontweight="bold")
            ax.legend(loc="upper left", frameon=False, fontsize=7.8, labelcolor=INK2)
        axes.flat[-1].axis("off")
        axes.flat[-1].text(0, 0.9, "Rule: 1h volume > 5x the 3-day average, green candle,\n"
                           f"EMA9 crosses above EMA21 ({'same candle' if window == 0 else 'within 6 candles'}),\n"
                           "stop = spike candle low, exit on the opposite EMA cross.\n\n"
                           "Risk 1% of equity per trade, max 3x size.\n"
                           "Delta fee 0.05% + 18% GST per side, slippage, funding.\n"
                           "All versions trade from 19-Jul-2020.",
                           transform=axes.flat[-1].transAxes, va="top", fontsize=10, color=INK2)
        fig.suptitle(f"Volume spike + EMA 9/21 — long only + daily trend filter, $10,000 per coin "
                     f"(window {window})", x=0.01, ha="left", fontsize=12.5, color=INK)
        fig.tight_layout()
        fig.savefig(OUT / f"volspike_trend_w{window}.png", dpi=125, facecolor=SURFACE)
    print("\ncharts:", OUT / "volspike_trend_w0.png", OUT / "volspike_trend_w6.png")


if __name__ == "__main__":
    main()
