import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Equity-curve charts for the crypto volume screen.

  results/equity_all_strategies_1d_pit.png  one panel per strategy: VOL vs no-volume
                                            twin (best in-sample VOL params, same params
                                            for the twin), each scaled to 20% ann. vol
  results/equity_xs_taker_delta.png         Delta sim at L=0.5 vs BTC, + drawdown panel

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.plot_equity
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

RES = Path(__file__).parent / "results"
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8984"
IS_END = pd.Timestamp("2023-12-31")
CAPITAL = 10_000
_usd = matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k" if v >= 1000 else f"${v:,.0f}")

NAMES = {
    "bench_ew_long": "Benchmark: hold all coins", "bench_btc_long": "Benchmark: hold BTC",
    "obv_trend": "OBV trend", "cvd_trend": "Buy/sell delta (CVD) trend",
    "vwap_trend": "Price vs rolling VWAP", "vwma_cross": "Volume-weighted MA cross",
    "force_index": "Force index", "cmf": "Chaikin money flow", "mfi_trend": "Money flow index",
    "donchian_vol": "Breakout + volume surge", "taker_breakout": "Breakout + buyer aggression",
    "spike_reversal": "Fade volume spikes", "spike_continuation": "Follow volume spikes",
    "cvd_divergence": "Delta divergence fade", "xs_mom": "Rank: momentum",
    "xs_taker": "Rank: taker-buy ratio", "xs_volsurge": "Rank: volume surge",
    "xs_attention": "Rank: fading attention",
}


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y"))


def _curve(d: pd.Series) -> pd.Series:
    d = d.loc[d.ne(0).idxmax():]
    s = d.std() * np.sqrt(365)
    return (1 + d * (0.20 / s)).cumprod() if s > 0 else d * 0 + 1


def all_strategies(tag="1d_v1_pit"):
    scr = pd.read_csv(RES / f"screen_{tag}.csv")
    daily = pd.read_parquet(RES / f"daily_{tag}.parquet")
    strats = list(dict.fromkeys(scr["strategy"]))
    cols = 4
    rows = int(np.ceil(len(strats) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(16, 3.1 * rows), facecolor=SURFACE)
    for ax, st in zip(axes.flat, strats):
        _style(ax)
        g = scr[scr["strategy"] == st]
        vol = g[g["use_volume"]]
        pick = (vol if len(vol) else g).loc[lambda x: x["sharpe_is"].idxmax()]
        prm = pick["params"]
        lines = []
        for uv, color, lbl in ((True, BLUE, "with volume"), (False, ORANGE, "no volume")):
            row = g[(g["params"] == prm) & (g["use_volume"] == uv)]
            if row.empty:
                continue
            key = f"{st}|{prm}|{'vol' if uv else 'base'}"
            eq = _curve(daily[key].fillna(0)) * CAPITAL
            ax.plot(eq.index, eq.values, color=color, lw=1.6, label=lbl)
            r = row.iloc[0]
            lines.append(f"{lbl}: Sharpe {r['sharpe']:.2f} (OOS {r['sharpe_oos']:.2f})")
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1, 2, 5)))
        ax.axvline(IS_END, color=GRAY, lw=0.8, ls=(0, (3, 3)))
        ax.set_title(NAMES.get(st, st), loc="left", fontsize=10.5, color=INK, fontweight="bold", pad=40)
        ax.text(0.0, 1.02, "\n".join(lines) + (f"\nparams {prm}" if prm != "{}" else ""),
                transform=ax.transAxes, va="bottom", fontsize=7.4, color=INK2)
        ax.yaxis.set_major_formatter(_usd)
        ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    for ax in axes.flat[len(strats):]:
        ax.axis("off")
    h = [plt.Line2D([], [], color=BLUE, lw=2), plt.Line2D([], [], color=ORANGE, lw=2),
         plt.Line2D([], [], color=GRAY, lw=1, ls=(0, (3, 3)))]
    fig.legend(h, ["with volume", "same rule, no volume", "in-sample | out-of-sample (2024)"],
               loc="upper left", bbox_to_anchor=(0.005, 0.983), ncol=3, frameon=False, fontsize=9.5,
               labelcolor=INK2)
    fig.suptitle(f"Crypto volume strategies — ${CAPITAL:,} start, daily bars, point-in-time top-100 Binance perps, "
                 "15 bps/side + funding, each curve scaled to 20% yearly volatility",
                 x=0.01, ha="left", fontsize=12, color=INK, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.965), h_pad=2.5)
    out = RES / "equity_all_strategies_1d_pit.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print(out)


def xs_taker_delta(L=0.5):
    r = pd.read_csv(RES / "sim_delta_xs_taker_daily.csv", index_col=0, parse_dates=True)["net_unit"]
    from .engine import Panel  # BTC benchmark from the same data
    btc = pd.read_parquet(Path(__file__).parents[2] / "data" / "CRYPTO_PERPS" / "BTCUSDT" / "klines_1h.parquet")
    btc = btc.set_index("datetime")["close"].resample("1D").last().loc[r.index[0]:r.index[-1]]
    eq = (1 + L * r).cumprod() * CAPITAL
    b = btc / btc.iloc[0] * CAPITAL
    dd = eq / eq.cummax() - 1
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 7), height_ratios=[3, 1.2], sharex=True,
                                 facecolor=SURFACE)
    for ax in (a1, a2):
        _style(ax)
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1, 7)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    a1.plot(eq.index, eq.values, color=BLUE, lw=2, label=f"xs_taker, {L}x per book (\\$5k long + \\$5k short)")
    a1.plot(b.index, b.values, color=ORANGE, lw=1.6, label="BTC buy & hold")
    a1.set_yscale("log")
    a1.yaxis.set_major_locator(matplotlib.ticker.FixedLocator([v * CAPITAL for v in (1, 1.5, 2, 3, 4, 5)]))
    a1.yaxis.set_major_formatter(_usd)
    a1.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    a1.text(eq.index[-1], eq.iloc[-1], f"  ${eq.iloc[-1]:,.0f}", color=INK, fontsize=9, va="center")
    a1.text(b.index[-1], b.iloc[-1], f"  ${b.iloc[-1]:,.0f}", color=INK, fontsize=9, va="center")
    a1.legend(loc="upper left", frameon=False, fontsize=9.5, labelcolor=INK2)
    yrs = len(r) / 365
    a1.set_title(f"xs_taker on Delta-tradable coins — $10,000 from Jan 2024  "
                 f"(CAGR {(eq.iloc[-1] / CAPITAL) ** (1 / yrs) - 1:.0%}, max drawdown {dd.min():.0%}, 15 bps/side + funding)",
                 loc="left", fontsize=11.5, color=INK, fontweight="bold")
    a2.fill_between(dd.index, dd.values, 0, color=BLUE, alpha=0.25, lw=0)
    a2.plot(dd.index, dd.values, color=BLUE, lw=1.2)
    a2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    a2.set_title("Drawdown", loc="left", fontsize=10, color=INK)
    fig.tight_layout()
    out = RES / "equity_xs_taker_delta.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print(out)


if __name__ == "__main__":
    xs_taker_delta()
    if (RES / "screen_1d_v1_pit.csv").exists():
        all_strategies()
