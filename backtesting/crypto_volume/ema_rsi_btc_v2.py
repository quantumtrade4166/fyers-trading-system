import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC EMA 9/21 cross + daily RSI(14) filter — fixing the fee problem (2026-10-24).

v1 (ema_rsi_btc.py): 1h candles, stop at the crossover candle low/high -> ~180
trades/yr on a tight stop = big positions = fees ate everything.
Here, everything else unchanged (daily RSI of the previous day > 50 longs only,
< 50 shorts only; exit on the opposite cross; $10k, 1% risk, max 3x; Delta 0.05%
+ 18% GST per side, 0.01% slippage, funding), we vary:
  timeframe  1h | 4h (UTC-aligned candles built from the 1h data)
  stop       'candle' crossover candle low/high | 'atrK' signal close -/+ K x ATR(14)
             of that timeframe (K = 1.5, 2, 3)

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.ema_rsi_btc_v2
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import volspike_ema as V
from .ema_rsi_btc import START, daily_rsi
from .plot_equity import SURFACE, INK, INK2, _style

OUT = Path(__file__).parent / "results"
STOPS = ["candle", "atr1.5", "atr2", "atr3"]


def to_tf(df1h: pd.DataFrame, tf: str) -> pd.DataFrame:
    if tf == "1h":
        return df1h
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "funding": "sum"}
    return df1h[list(agg)].resample(tf, label="left", closed="left").agg(agg).dropna(subset=["close"])


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def signals(df: pd.DataFrame, stop: str) -> pd.DataFrame:
    sig = V.signals(df, use_volume=False)                # EMA 9/21 cross; stop = candle low/high
    rsi = daily_rsi(df)
    sig["long"] &= rsi > 50
    sig["short"] &= rsi < 50
    if stop.startswith("atr"):
        k = float(stop[3:])
        a = atr(df)
        sig["stop_long"] = df["close"] - k * a
        sig["stop_short"] = df["close"] + k * a
    sig.loc[sig.index < START, ["long", "short"]] = False
    return sig


def run(df, stop):
    tr, cv = V.backtest(df, signals(df, stop), "cross")
    return tr, cv.loc[START:]


def main() -> None:
    d1 = V.load("BTCUSDT")
    res = {}
    print(f"{'version':<22}{'trades':>7}{'/yr':>5}{'win%':>6}{'avgR':>7}{'PF':>6}{'end $':>9}{'CAGR':>8}"
          f"{'maxDD':>8}{'Sharpe':>7}{'fees $':>9}{'long $':>9}{'short $':>9}{'yrs up':>8}")
    for tf in ("1h", "4h"):
        df = to_tf(d1, tf)
        for stop in STOPS:
            tr, cv = run(df, stop)
            m = V.report(tr, cv)
            d = cv.resample("1D").last()
            yr = d.groupby(d.index.year).last() / d.groupby(d.index.year).first() - 1
            name = f"{tf} stop {stop}"
            res[name] = (tr, d, m, yr)
            print(f"{name:<22}{m['trades']:>7}{m['per_year']:>5.0f}{m['win%']:>6.0%}{m['avgR']:>+7.2f}{m['PF']:>6.2f}"
                  f"{m['end$']:>9,.0f}{m['CAGR']:>+8.1%}{m['maxDD']:>8.1%}{m['Sharpe']:>7.2f}{-m['fees$']:>+9,.0f}"
                  f"{m['long$']:>+9,.0f}{m['short$']:>+9,.0f}{f'{(yr > 0).sum()}/{len(yr)}':>8}", flush=True)

    print("\n-- 4h, by year --")
    for stop in STOPS:
        yr = res[f"4h stop {stop}"][3]
        print(f"  {stop:<7}" + "  ".join(f"{y}: {v:+6.1%}" for y, v in yr.items()))
    print("\n-- dependence on the best trades --")
    for name, (tr, *_ ) in res.items():
        p = sorted((t.pnl for t in tr), reverse=True)
        print(f"  {name:<20} best 3 ${sum(p[:3]):>+8,.0f} | other {len(p) - 3:>4} trades ${sum(p[3:]):>+9,.0f}")

    print("\n-- same 4h versions with ZERO fees/slippage (is there any raw edge?) --")
    fee, slip = V.FEE, V.SLIP
    V.FEE, V.SLIP = 0.0, 0.0
    df4 = to_tf(d1, "4h")
    for stop in STOPS:
        tr, cv = run(df4, stop)
        m = V.report(tr, cv)
        print(f"  4h stop {stop:<7} end ${m['end$']:>9,.0f}  CAGR {m['CAGR']:+6.1%}  maxDD {m['maxDD']:6.1%}")
    V.FEE, V.SLIP = fee, slip

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7.5), height_ratios=[3, 1.2], sharex=True, facecolor=SURFACE)
    cols = {"1h stop candle": "#8a8984", "4h stop candle": "#eda100", "4h stop atr1.5": "#eb6834",
            "4h stop atr2": "#2a78d6", "4h stop atr3": "#1baf7a"}
    for ax in (a1, a2):
        _style(ax)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for name, c in cols.items():
        d = res[name][1]
        a1.plot(d.index, d.values, color=c, lw=1.4, label=f"{name}  ${d.iloc[-1]:,.0f}")
        a2.plot(d.index, (d / d.cummax() - 1).values, color=c, lw=1.1)
    a1.axhline(10_000, color=INK2, lw=0.8)
    a1.set_yscale("log")
    a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.1f}k"))
    a1.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    a1.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=INK2)
    a1.set_title("BTC EMA 9/21 + daily RSI filter — 1h vs 4h, candle vs ATR stop — $10,000, 1% risk, Delta charges",
                 loc="left", fontsize=11.5, color=INK, fontweight="bold")
    a2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    a2.set_title("Drawdown", loc="left", fontsize=10, color=INK)
    fig.tight_layout()
    out = OUT / "ema_rsi_btc_v2.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print("\n" + str(out))


if __name__ == "__main__":
    main()
