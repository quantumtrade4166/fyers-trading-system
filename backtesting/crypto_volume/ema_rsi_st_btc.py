import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC EMA 9/21 cross + Supertrend(10, 3) direction filter (owner, 2026-10-24).

Same rule and money as ema_rsi_btc_v2.py (exit on the opposite cross, $10k, 1% risk,
max 3x, Delta 0.05% + 18% GST + slippage, funding). Filters (longs only when the
filter says up, shorts only when it says down):
  RSI        daily RSI(14) of the previous day vs 50          (the earlier version)
  ST-daily   daily Supertrend(10,3) of the previous completed day
  ST-chart   Supertrend(10,3) on the trading candles themselves (known at the close)
  RSI+ST-daily, RSI+ST-chart   both must agree
Supertrend = backtesting/indicators.add_supertrend (Wilder ATR, TradingView-style bands).
Grid: 1h | 4h  x  stop 'candle' | 'atr2'.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.ema_rsi_st_btc
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from backtesting.indicators import add_supertrend
from . import volspike_ema as V
from .ema_rsi_btc import START, daily_rsi
from .ema_rsi_btc_v2 import atr, to_tf
from .plot_equity import SURFACE, INK, INK2, _style

OUT = Path(__file__).parent / "results"
FILTERS = ["RSI", "ST-daily", "ST-chart", "RSI+ST-daily", "RSI+ST-chart"]


def st_dir(df: pd.DataFrame) -> pd.Series:
    return add_supertrend(df[["open", "high", "low", "close"]].copy(), 10, 3.0)["supertrend_dir_10_3.0"]


def st_daily(df: pd.DataFrame) -> pd.Series:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    d = df[list(agg)].resample("1D").agg(agg).dropna()
    s = st_dir(d).shift(1)                                    # yesterday's completed day
    return pd.Series(s.reindex(df.index.floor("D")).to_numpy(), index=df.index)


def signals(df: pd.DataFrame, filt: str, stop: str) -> pd.DataFrame:
    sig = V.signals(df, use_volume=False)                     # EMA 9/21 cross
    up = pd.Series(True, index=df.index)
    dn = pd.Series(True, index=df.index)
    if "RSI" in filt:
        r = daily_rsi(df)
        up &= r > 50
        dn &= r < 50
    if "ST-daily" in filt:
        s = st_daily(df)
        up &= s == 1
        dn &= s == -1
    if "ST-chart" in filt:
        s = st_dir(df)
        up &= s == 1
        dn &= s == -1
    sig["long"] &= up
    sig["short"] &= dn
    if stop == "atr2":
        a = atr(df)
        sig["stop_long"], sig["stop_short"] = df["close"] - 2 * a, df["close"] + 2 * a
    sig.loc[sig.index < START, ["long", "short"]] = False
    return sig


def main() -> None:
    d1 = V.load("BTCUSDT")
    frames = {tf: to_tf(d1, tf) for tf in ("1h", "4h")}
    res = {}
    print(f"{'version':<30}{'trades':>7}{'/yr':>5}{'win%':>6}{'PF':>6}{'end $':>9}{'CAGR':>8}{'maxDD':>8}"
          f"{'Sharpe':>7}{'fees $':>8}{'long $':>9}{'short $':>9}{'yrs up':>7}{'w/o best3':>10}")
    for tf, df in frames.items():
        for stop in ("candle", "atr2"):
            for filt in FILTERS:
                tr, cv = V.backtest(df, signals(df, filt, stop), "cross")
                cv = cv.loc[START:]
                m = V.report(tr, cv)
                d = cv.resample("1D").last()
                yr = d.groupby(d.index.year).last() / d.groupby(d.index.year).first() - 1
                p = sorted((t.pnl for t in tr), reverse=True)
                name = f"{tf} {stop:<6} {filt}"
                res[name] = (d, m, yr)
                print(f"{name:<30}{m['trades']:>7}{m['per_year']:>5.0f}{m['win%']:>6.0%}{m['PF']:>6.2f}"
                      f"{m['end$']:>9,.0f}{m['CAGR']:>+8.1%}{m['maxDD']:>8.1%}{m['Sharpe']:>7.2f}"
                      f"{-m['fees$']:>+8,.0f}{m['long$']:>+9,.0f}{m['short$']:>+9,.0f}"
                      f"{f'{(yr > 0).sum()}/{len(yr)}':>7}{sum(p[3:]):>+10,.0f}", flush=True)
            print()

    best = max(res, key=lambda k: res[k][1]["Sharpe"])
    print(f"best Sharpe: {best}")
    print("  by year: " + "  ".join(f"{y}: {v:+.1%}" for y, v in res[best][2].items()))
    fee, slip = V.FEE, V.SLIP
    V.FEE = V.SLIP = 0.0
    tf, stop, filt = best.split()
    tr, cv = V.backtest(frames[tf], signals(frames[tf], filt, stop), "cross")
    m0 = V.report(tr, cv.loc[START:])
    print(f"  same with ZERO fees: end ${m0['end$']:,.0f}, CAGR {m0['CAGR']:+.1%}, maxDD {m0['maxDD']:.1%}")
    V.FEE, V.SLIP = fee, slip

    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7.5), height_ratios=[3, 1.2], sharex=True, facecolor=SURFACE)
    show = ["4h atr2   RSI", "4h atr2   ST-daily", "4h atr2   ST-chart", "4h atr2   RSI+ST-daily",
            "4h atr2   RSI+ST-chart"]
    cols = ["#8a8984", "#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
    for ax in (a1, a2):
        _style(ax)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for name, c in zip(show, cols):
        d = res[name][0]
        a1.plot(d.index, d.values, color=c, lw=1.5, label=f"{name.replace('4h atr2   ', '4h, ATR stop, ')}  ${d.iloc[-1]:,.0f}")
        a2.plot(d.index, (d / d.cummax() - 1).values, color=c, lw=1.1)
    a1.axhline(10_000, color=INK2, lw=0.8)
    a1.yaxis.set_major_locator(matplotlib.ticker.MultipleLocator(2000))
    a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.1f}k"))
    a1.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    a1.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK2)
    a1.set_title("BTC EMA 9/21 cross — RSI vs Supertrend(10,3) filters (4h, 2xATR stop) — $10,000, 1% risk, Delta charges",
                 loc="left", fontsize=11, color=INK, fontweight="bold")
    a2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    a2.set_title("Drawdown", loc="left", fontsize=10, color=INK)
    fig.tight_layout()
    out = OUT / "ema_rsi_st_btc.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print(str(out))


if __name__ == "__main__":
    main()
