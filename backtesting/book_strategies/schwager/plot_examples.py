import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Chart examples:
  1. 2.55 narrow range near the top of the 60-day range (NSE stocks, ETFs removed)
  2. the two suspicious flag losses: DAAWAT (Jan-2017) and BOROSIL (Jul-2018)
    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.schwager.plot_examples
"""

from multiprocessing import Pool
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from backtesting.engine import bhav, bhav_fix
from backtesting.book_strategies.schwager import strategies as S
from backtesting.book_strategies.schwager.stock_universe import stock_symbols

NT = {"L": 5, "w": 3.0, "sides": "long"}
OUT = Path(__file__).resolve().parent / "results"


def _load(sym):
    df = bhav_fix.load(sym)
    return df[df["close"] > 0]


def _trades(pos):
    out, t, n = [], 1, len(pos)
    while t < n:
        if pos[t] > 0 and pos[t - 1] == 0:
            e = t
            while e + 1 < n and pos[e + 1] > 0:
                e += 1
            out.append((t, e)); t = e + 1
        else:
            t += 1
    return out


def nt_trades(sym):
    try:
        df = _load(sym)
    except Exception:
        return []
    if len(df) < 300 or not df["tradable"].any():
        return []
    d = S.prep(df)
    pos = np.where(df["tradable"].to_numpy(bool), np.clip(S.narrow_top(d, NT), 0, None), 0.0)
    rh, rl, _ = S._ranges(d, NT["L"], NT["w"])
    bh = S._prev(S._rmax(d["h"], 60)); bl = S._prev(S._rmin(d["l"], 60))
    res = []
    for s, e in _trades(pos):
        ent = d["o"][s + 1] if s + 1 < len(df) else np.nan
        ex = d["o"][e + 1] if e + 1 < len(df) else d["c"][e]
        res.append({"sym": sym, "s": s, "e": e, "date": df.index[s], "ret": ex / ent - 1,
                    "bh": rh[s], "bl": rl[s], "BH": bh[s], "BL": bl[s]})
    return res


def candles(ax, x):
    xs = np.arange(len(x))
    up = x["close"] >= x["open"]
    ax.vlines(xs, x["low"], x["high"], color="#555", lw=0.8)
    ax.bar(xs, (x["close"] - x["open"]).abs().clip(lower=x["close"] * 1e-4),
           bottom=np.minimum(x["open"], x["close"]), color=np.where(up, "#2a9d55", "#d1495b"), width=0.7)
    step = max(1, len(x) // 6)
    ax.set_xticks(xs[::step]); ax.set_xticklabels([f"{t:%d %b %y}" for t in x.index[::step]], fontsize=7)
    ax.tick_params(axis="y", labelsize=7)
    return xs


def mark(ax, x, i, label, color, dy):
    ax.annotate(label, (i, x["open"].iloc[i]), xytext=(0, dy), textcoords="offset points", ha="center",
                color=color, fontsize=8, arrowprops=dict(arrowstyle="->", color=color))


def draw_nt(ax, tr):
    df = _load(tr["sym"])
    s, e, L = tr["s"], tr["e"], NT["L"]
    a, b = max(0, s - 65), min(len(df), e + 15)
    x = df.iloc[a:b]
    candles(ax, x)
    ax.axhspan(tr["BL"] + 0.75 * (tr["BH"] - tr["BL"]), tr["BH"], color="#f4a261", alpha=0.12)
    ax.hlines([tr["BH"], tr["BL"]], s - 60 - a, s - a, color="#f4a261", lw=1, ls="--")
    fs = s - L - a
    ax.add_patch(plt.Rectangle((fs - 0.5, tr["bl"]), L, tr["bh"] - tr["bl"], alpha=0.18, color="#1f6fb2", lw=0))
    ax.hlines([tr["bh"], tr["bl"]], fs - 0.5, s - a - 0.5, color="#1f6fb2", lw=1)
    mark(ax, x, min(s + 1, len(df) - 1) - a, "BUY", "#2a9d55", -28)
    mark(ax, x, min(e + 1, len(df) - 1) - a, "EXIT", "#d1495b", 28)
    ax.set_title(f"{tr['sym']}  {tr['date']:%d-%b-%Y}  →  {tr['ret']*100:+.1f}%  ({e - s} days)", fontsize=9)


def draw_case(ax, sym, date, note):
    df = _load(sym)
    i = df.index.get_indexer([pd.Timestamp(date)], method="nearest")[0]
    x = df.iloc[max(0, i - 40):min(len(df), i + 40)]
    xs = candles(ax, x)
    j = i - max(0, i - 40)
    ax.axvline(j, color="#1f6fb2", ls=":", lw=1)
    big = x["close"].pct_change().abs().idxmax()
    k = x.index.get_loc(big)
    ax.annotate(f"{x['close'].pct_change().loc[big]*100:+.0f}% in one day\n{big:%d-%b-%Y}", (k, x["close"].iloc[k]),
                xytext=(25, 20), textcoords="offset points", fontsize=8, color="#b00",
                arrowprops=dict(arrowstyle="->", color="#b00"))
    ax.set_title(f"{sym} — flag BUY {pd.Timestamp(date):%d-%b-%Y} (dotted line). {note}", fontsize=9)


if __name__ == "__main__":
    with Pool(12) as p:
        T = pd.DataFrame([t for part in p.map(nt_trades, stock_symbols()) for t in part])
    T = T[T["date"] >= "2017-01-01"]
    print(f"2.55: {len(T)} trades 2017-2026: win {100*(T.ret>0).mean():.0f}%, avg {100*T.ret.mean():+.2f}%, "
          f"median {100*T.ret.median():+.2f}%, best {100*T.ret.max():+.0f}%, worst {100*T.ret.min():+.0f}%")
    top10 = T.nlargest(10, "ret").ret.sum() / T.ret.sum()
    print(f"  top-10 trades = {100*top10:.0f}% of the summed trade returns")
    T.sort_values("date").to_csv(OUT / "narrowtop_trades_stocks.csv", index=False)
    print(T.nsmallest(5, "ret")[["sym", "date", "ret"]].to_string(index=False))
    picks = pd.concat([T.nlargest(1, "ret"), T[T.ret > 0].sample(3, random_state=3), T[T.ret <= 0].sample(4, random_state=3)])
    fig, axes = plt.subplots(4, 2, figsize=(14, 15))
    for ax, (_, tr) in zip(axes.ravel(), picks.iterrows()):
        draw_nt(ax, tr)
    fig.suptitle("2.55 Narrow range near top — orange band = top 25% of the 60-day range, blue box = 5-day narrow range,\n"
                 "BUY = next open after a close above the box.   Row 1-2: winners (incl. biggest)   Row 3-4: losers (random)", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.95]); fig.savefig(OUT / "narrowtop_examples.png", dpi=110)

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    draw_case(axes[0], "DAAWAT", "2017-01-30", "Backtest: −90%")
    draw_case(axes[1], "BOROSIL", "2018-07-24", "Backtest: −75%")
    fig.tight_layout(); fig.savefig(OUT / "flag_bad_cases.png", dpi=110)
    print("saved")
