import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #2 — the four tests that don't fit the one-strategy-per-market grid.

    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.schwager.run_extra [45] [49] [52] [57]

2.45  Diversify across system variations (Ch 16): breakout N=20/40/60/100
      traded together vs N=40 alone.
2.49  The optimisation test (Ch 19): do the best breakout lengths of the
      past 8 years stay best in the next 2? (book: no)
2.52  Ratio spreads (Ch 31-33, rule 65): trend-follow the ratio of two
      related markets, long one leg / short the other, equal value.
2.57  Rule 66 trend-day close: if a market has traded above its open for
      most of the session, buy for the close (intraday, 5-min).
"""

from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.book_strategies.schwager import data as D, strategies as S
from backtesting.book_strategies.schwager.run_daily import (
    market_returns, vol_weight, to_bdays, stats, OOS_START, TARGET_VOL, _ann)

OUT = Path(__file__).resolve().parent / "results"


def _bo_port(markets, N):
    cols = {}
    for m in markets:
        df = D.load(m); d = S.prep(df)
        ret, _ = market_returns(m, S.breakout(d, {"N": N}), df)
        cols[m] = to_bdays(ret * vol_weight(df, m))
    return pd.DataFrame(cols).sort_index().mean(axis=1, skipna=True)


def t45(markets):
    ports = {N: _bo_port(markets, N) for N in (20, 40, 60, 100)}
    mix = pd.concat(ports.values(), axis=1).mean(axis=1)
    rows = []
    for lab, s in [*[(f"N={N} alone", p) for N, p in ports.items()], ("all four mixed", mix)]:
        a, b = stats(s[s.index < OOS_START]), stats(s[s.index >= OOS_START])
        rows.append({"variant": lab, "IS_sharpe": a["sharpe"], "OOS_sharpe": b["sharpe"],
                     "OOS_cagr": b["cagr"], "OOS_maxdd": b["maxdd"], "OOS_vol": b["vol"]})
    t = pd.DataFrame(rows)
    t.to_csv(OUT / "extra_2_45_variations.csv", index=False)
    print("\n2.45 diversification across breakout lengths\n" + t.round(3).to_string(index=False))


def t49(markets):
    Ns = list(range(20, 101, 10))
    out = []
    for m in markets:
        df = D.load(m); d = S.prep(df)
        R = pd.DataFrame({N: market_returns(m, S.breakout(d, {"N": N}), df)[0] for N in Ns})
        yrs = sorted(R.index.year.unique())
        for y in range(yrs[0] + 8, yrs[-1], 2):
            past = R[(R.index.year >= y - 8) & (R.index.year < y)]
            nxt = R[(R.index.year >= y) & (R.index.year < y + 2)]
            if len(nxt) < 300 or past.index.year.nunique() < 8:
                continue
            pp, np_ = past.sum(), nxt.sum()             # P/L, as in the book's tables
            rk_next = np_.rank(ascending=False)
            best_past = pp.idxmax()
            out.append({"market": m, "test": f"{y}-{y+1}", "spearman": pp.corr(np_, method="spearman"),
                        "rank_of_past_best": rk_next[best_past]})
    t = pd.DataFrame(out)
    t.to_csv(OUT / "extra_2_49_optimisation.csv", index=False)
    print(f"\n2.49 optimisation test — {len(t)} market x 2-year windows")
    print(f"  avg rank (1=best..9=worst) of the past-8-year best N in the next 2 years: "
          f"{t['rank_of_past_best'].mean():.2f}  (pure luck = 5.0)")
    print(f"  avg Spearman past-vs-next ranking: {t['spearman'].mean():+.3f}  (0 = no persistence)")
    print(t.groupby("market")[["spearman", "rank_of_past_best"]].mean().round(2).to_string())


PAIRS = [  # (A, B, sB): ratio = A * B**sB ; legs: A pos, B pos*sB
    ("NIFTY", "BANKNIFTY", -1), ("SP500", "NASDAQ100", -1), ("GOLD", "SILVER", -1),
    ("BRENT", "WTI", -1), ("EURUSD", "GBPUSD", -1), ("EURUSD", "USDCHF", 1), ("AUDUSD", "USDCAD", 1),
]


def t52(markets):
    res = {}
    for N in (20, 50, 100):
        cols = {}
        for A, B, sB in PAIRS:
            if A not in markets or B not in markets:
                continue
            a, b = D.load(A), D.load(B)
            idx = a.index.intersection(b.index)
            a, b = a.loc[idx], b.loc[idx]
            ratio = (a["close"] * b["close"] ** sB).to_frame("close")
            for k in ("open", "high", "low"):
                ratio[k] = ratio["close"]           # breakout on ratio CLOSES
            d = S.prep(ratio)
            pos = S.breakout(d, {"N": N})
            ra, _ = market_returns(A, pos, a)
            rb, _ = market_returns(B, pos * sB, b)
            sp = ra + rb
            v = sp.rolling(60, min_periods=40).std().shift(1) * np.sqrt(252)
            cols[f"{A}/{B}"] = to_bdays(sp * (TARGET_VOL / v).clip(upper=10))
        P = pd.DataFrame(cols).sort_index()
        res[N] = P
    rows = []
    for N, P in res.items():
        port = P.mean(axis=1, skipna=True)
        a, b = stats(port[port.index < OOS_START]), stats(port[port.index >= OOS_START])
        r = {"N": N, "IS_sharpe": a["sharpe"], "OOS_sharpe": b["sharpe"], "OOS_cagr": b["cagr"], "OOS_maxdd": b["maxdd"]}
        for c in P.columns:
            s = P[c]; r[f"OOS {c}"] = stats(s[s.index >= OOS_START])["sharpe"]
        rows.append(r)
    t = pd.DataFrame(rows)
    t.to_csv(OUT / "extra_2_52_ratio_spreads.csv", index=False)
    print("\n2.52 ratio-spread breakout (equal-value legs)\n" + t.round(2).to_string(index=False))


def t57():
    from backtesting.engine import universe
    from backtesting.book_strategies.kaufman.data_btc import get_bars
    sets = [("NIFTY", universe.load_in_index("NSE_NIFTY50_INDEX"), "14:00", 0.0002, "2025-09-01"),
            ("BANKNIFTY", universe.load_in_index("NSE_NIFTYBANK_INDEX"), "14:00", 0.0002, "2025-09-01"),
            ("BTC", get_bars("5min"), "20:00", 0.0006, "2023-01-01")]
    rows = []
    for name, df, cut, cost, oos in sets:
        df = df[["open", "high", "low", "close"]].copy()
        day = df.index.normalize()
        g = df.groupby(day)
        d_open = g["open"].transform("first")
        above = (df["close"] > d_open).astype(float)
        frac = above.groupby(day).cumsum() / (g.cumcount() + 1)
        at = df.index.strftime("%H:%M") == pd.Timestamp(cut).strftime("%H:%M")
        snap = pd.DataFrame({"px": df["close"], "frac": frac, "open": d_open})[at]
        last = g["close"].last()
        snap["eod"] = last.reindex(snap.index.normalize()).to_numpy()
        for q in (0.75, 0.9):
            sig = np.where((snap["frac"] >= q) & (snap["px"] > snap["open"]), 1,
                           np.where((snap["frac"] <= 1 - q) & (snap["px"] < snap["open"]), -1, 0))
            r = pd.Series(sig * (snap["eod"] / snap["px"] - 1) - 2 * cost * (sig != 0), index=snap.index)
            tr = r[sig != 0]
            for lab, s in (("IS", tr[tr.index < oos]), ("OOS", tr[tr.index >= oos])):
                rows.append({"market": name, "q": q, "period": lab, "trades": len(s),
                             "win_pct": 100 * (s > 0).mean() if len(s) else np.nan,
                             "avg_pct": 100 * s.mean() if len(s) else np.nan,
                             "total_pct": 100 * s.sum()})
    t = pd.DataFrame(rows)
    t.to_csv(OUT / "extra_2_57_trend_day_close.csv", index=False)
    print("\n2.57 trend-day close (buy at cut-off time, sell at session close)\n" + t.round(3).to_string(index=False))


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    which = sys.argv[1:] or ["45", "49", "52", "57"]
    mk = D.available()
    for w in which:
        {"45": lambda: t45(mk), "49": lambda: t49(mk), "52": lambda: t52(mk), "57": t57}[w]()
