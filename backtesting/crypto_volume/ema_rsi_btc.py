import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC 1h EMA 9/21 crossover with a DAILY RSI filter — the owner's rule (2026-10-24).

Entry : EMA9 crosses above EMA21 on a 1h candle -> LONG, only if the daily RSI(14)
        of the PREVIOUS completed UTC day is above 50. EMA9 crosses below EMA21 ->
        SHORT, only if that daily RSI is below 50. Enter at the next candle's open.
Stop  : long  -> the crossover candle's LOW; short -> the crossover candle's HIGH.
Exits : 'cross' opposite EMA cross (a stop-and-reverse when the RSI allows the
        other side), 'R2' / 'R3' fixed targets. Stop always live; stop and target in
        the same candle = stop; a gap through the stop fills at the open.
Money : $10,000, 1% of equity risked per trade, notional capped at 3x equity,
        Delta futures 0.05% + 18% GST per side, 0.01% slippage, Binance funding.
Engine: backtesting/crypto_volume/volspike_ema.py (backtest(), report()).

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.ema_rsi_btc
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import volspike_ema as V
from .plot_equity import SURFACE, INK, INK2, _style

OUT = Path(__file__).parent / "results"
START = pd.Timestamp("2020-02-01")          # first day with a 14-day daily RSI


def daily_rsi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder RSI on UTC daily closes, shifted one day (yesterday's value), on the 1h index."""
    d = df["close"].resample("1D").last().dropna()
    ch = d.diff()
    up = ch.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-ch.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rsi = (100 - 100 / (1 + up / dn)).shift(1)
    return pd.Series(rsi.reindex(df.index.floor("D")).to_numpy(), index=df.index)


def signals(df: pd.DataFrame, use_rsi: bool = True) -> pd.DataFrame:
    sig = V.signals(df, use_volume=False)        # EMA 9/21 cross, stop = candle low/high
    if use_rsi:
        rsi = daily_rsi(df)
        sig["long"] &= rsi > 50
        sig["short"] &= rsi < 50
    sig.loc[sig.index < START, ["long", "short"]] = False
    return sig


def main() -> None:
    df = V.load("BTCUSDT")
    print(f"BTC perp 1h {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}; trades from {START:%Y-%m-%d}")
    print(f"{'version':<34}{'trades':>7}{'/yr':>5}{'win%':>6}{'avgR':>7}{'PF':>6}{'end $':>9}"
          f"{'CAGR':>8}{'maxDD':>8}{'Sharpe':>7}{'fees $':>8}{'long $':>9}{'short $':>9}")
    results = {}
    for use_rsi in (True, False):
        for ex in ("cross", "R2", "R3"):
            tr, cv = V.backtest(df, signals(df, use_rsi), ex)
            cv = cv.loc[START:]
            m = V.report(tr, cv)
            name = f"{'RSI filter' if use_rsi else 'NO filter'}, exit {ex}"
            results[name] = (tr, cv, m)
            print(f"{name:<34}{m['trades']:>7}{m['per_year']:>5.0f}{m['win%']:>6.0%}{m['avgR']:>+7.2f}"
                  f"{m['PF']:>6.2f}{m['end$']:>9,.0f}{m['CAGR']:>+8.1%}{m['maxDD']:>8.1%}{m['Sharpe']:>7.2f}"
                  f"{-m['fees$']:>+8,.0f}{m['long$']:>+9,.0f}{m['short$']:>+9,.0f}", flush=True)

    print("\n-- by year (RSI filter) --")
    for ex in ("cross", "R2", "R3"):
        tr, cv, _ = results[f"RSI filter, exit {ex}"]
        d = cv.resample("1D").last()
        yr = d.groupby(d.index.year).last() / d.groupby(d.index.year).first() - 1
        print(f"  exit {ex:<6}" + "  ".join(f"{y}: {v:+.1%}" for y, v in yr.items()))
    for ex in ("cross", "R2", "R3"):
        tr = results[f"RSI filter, exit {ex}"][0]
        p = sorted((t.pnl for t in tr), reverse=True)
        print(f"  exit {ex:<6} best 3 trades ${sum(p[:3]):+,.0f}, all other {len(p) - 3} trades ${sum(p[3:]):+,.0f}")

    # chart
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7.5), height_ratios=[3, 1.2], sharex=True, facecolor=SURFACE)
    cols = {"RSI filter, exit cross": "#2a78d6", "RSI filter, exit R2": "#eb6834",
            "RSI filter, exit R3": "#1baf7a", "NO filter, exit cross": "#8a8984"}
    for ax in (a1, a2):
        _style(ax)
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for name, c in cols.items():
        cv = results[name][1].resample("1D").last()
        a1.plot(cv.index, cv.values, color=c, lw=2 if "cross" in name and "RSI" in name else 1.4,
                label=f"{name}  ${cv.iloc[-1]:,.0f}")
        dd = cv / cv.cummax() - 1
        a2.plot(dd.index, dd.values, color=c, lw=1.2)
    a1.axhline(10_000, color=INK2, lw=0.8)
    a1.set_yscale("log")
    a1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.1f}k"))
    a1.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    a1.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=INK2)
    a1.set_title("BTC 1h EMA 9/21 cross + daily RSI(14) filter — $10,000, 1% risk per trade, Delta charges",
                 loc="left", fontsize=11.5, color=INK, fontweight="bold")
    a2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    a2.set_title("Drawdown", loc="left", fontsize=10, color=INK)
    fig.tight_layout()
    out = OUT / "ema_rsi_btc.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print("\n" + str(out))


if __name__ == "__main__":
    main()
