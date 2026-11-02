import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Chart examples of 2.13 flag trades on NSE stocks (same rules as the backtest).
    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.schwager.plot_flag_examples
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

CFG = {"F": 7, "w": 2.5, "pole": 5.0, "sides": "long"}
OUT = Path(__file__).resolve().parent / "results"


def trades_for(sym):
    try:
        df = bhav_fix.load(sym)
    except Exception:
        return []
    df = df[df["close"] > 0]
    if len(df) < 300 or not df["tradable"].any():
        return []
    d = S.prep(df)
    trad = df["tradable"].to_numpy(bool)
    pos = np.where(trad, np.clip(S.flag(d, CFG), 0, None), 0.0)
    fh, fl, upf, _ = S._flags(d, CFG["F"], CFG["w"], CFG["pole"])
    out = []
    t = 1
    n = len(pos)
    while t < n:
        if pos[t] > 0 and pos[t - 1] == 0:
            s = t                                   # signal bar (close)
            e = s
            while e + 1 < n and pos[e + 1] > 0:
                e += 1
            ent = d["o"][s + 1] if s + 1 < n else np.nan     # next open
            ex = d["o"][e + 1] if e + 1 < n else d["c"][e]
            out.append({"sym": sym, "s": s, "e": e, "date": df.index[s], "ret": ex / ent - 1,
                        "fh": fh[s], "fl": fl[s]})
            t = e + 1
        else:
            t += 1
    return out


def draw(ax, tr):
    df = bhav_fix.load(tr["sym"]); df = df[df["close"] > 0]
    s, e, F = tr["s"], tr["e"], CFG["F"]
    a, b = max(0, s - F - 25), min(len(df), e + 15)
    x = df.iloc[a:b]
    xs = np.arange(len(x))
    up = x["close"] >= x["open"]
    ax.vlines(xs, x["low"], x["high"], color="#555", lw=0.8)
    ax.bar(xs, (x["close"] - x["open"]).abs().clip(lower=x["close"] * 1e-4),
           bottom=np.minimum(x["open"], x["close"]), color=np.where(up, "#2a9d55", "#d1495b"), width=0.7)
    fs, fe = s - F - a, s - 1 - a                          # flag days
    ax.add_patch(plt.Rectangle((fs - 0.5, tr["fl"]), fe - fs + 1, tr["fh"] - tr["fl"],
                               fill=True, alpha=0.15, color="#1f6fb2", lw=0))
    ax.hlines([tr["fh"], tr["fl"]], fs - 0.5, fe + 0.5, color="#1f6fb2", lw=1)
    ps = s - F - 10 - a                                    # pole window start
    ax.axvspan(max(ps, 0) - 0.5, fs - 0.5, color="#f4a261", alpha=0.12)
    ax.annotate("BUY", (s + 1 - a, x["open"].iloc[s + 1 - a]), xytext=(0, -28), textcoords="offset points",
                ha="center", color="#2a9d55", fontsize=8, arrowprops=dict(arrowstyle="->", color="#2a9d55"))
    xi = min(e + 1, len(df) - 1) - a
    ax.annotate("EXIT", (xi, x["open"].iloc[xi]), xytext=(0, 28), textcoords="offset points",
                ha="center", color="#d1495b", fontsize=8, arrowprops=dict(arrowstyle="->", color="#d1495b"))
    ax.set_title(f"{tr['sym']}  {tr['date']:%d-%b-%Y}  →  {tr['ret']*100:+.1f}%  ({e - s} days)", fontsize=9)
    step = max(1, len(x) // 6)
    ax.set_xticks(xs[::step]); ax.set_xticklabels([f"{t:%d %b}" for t in x.index[::step]], fontsize=7)
    ax.tick_params(axis="y", labelsize=7)


if __name__ == "__main__":
    with Pool(12) as p:
        allt = [t for part in p.map(trades_for, __import__("backtesting.book_strategies.schwager.stock_universe", fromlist=["x"]).stock_symbols()) for t in part]
    T = pd.DataFrame(allt)
    T = T[T["date"] >= "2017-01-01"]
    print(f"{len(T)} flag trades 2017-2026: win {100*(T.ret>0).mean():.0f}%, avg {100*T.ret.mean():+.2f}%, "
          f"median {100*T.ret.median():+.2f}%, best {100*T.ret.max():+.0f}%, worst {100*T.ret.min():+.0f}%")
    rng = np.random.default_rng(7)
    win = T[T.ret > 0].sample(4, random_state=7); los = T[T.ret <= 0].sample(4, random_state=7)
    big = T.nlargest(1, "ret")
    picks = pd.concat([big, win.head(3), los])
    fig, axes = plt.subplots(4, 2, figsize=(14, 15))
    for ax, (_, tr) in zip(axes.ravel(), picks.iterrows()):
        draw(ax, tr)
    fig.suptitle("2.13 Flag/pennant on NSE stocks — orange = pole window, blue box = flag, BUY = next open after close above box\n"
                 "Row 1-2: winners (incl. biggest)   Row 3-4: losers (random)", fontsize=11)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    f = OUT / "flag_examples.png"
    fig.savefig(f, dpi=110)
    T.sort_values("date").to_csv(OUT / "flag_trades_stocks.csv", index=False)
    print(f)
