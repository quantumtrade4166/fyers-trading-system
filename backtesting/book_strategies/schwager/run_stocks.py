import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #2 on Indian stocks — survivorship-free NSE daily (bhavcopy, every
stock incl. delisted), point-in-time top-500 by traded value.

    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.schwager.run_stocks

* LONG side only (cash delivery; overnight shorts need futures)
* each strategy uses the setting chosen on the FUTURES in-sample run
  (results/daily_selected.csv), so stocks are a fully independent test
* a stock is only held while it is in that day's top 500
* delivery costs 0.15%/side; each stock scaled to 10% vol (cap 5x)
* portfolio = average over the day's top-500 stocks (flat stocks count 0)
* reported for 2007-2016 and 2017-2026 separately
"""

import ast
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.engine import bhav
from backtesting.book_strategies.schwager import strategies as S
from backtesting.book_strategies.schwager.run_daily import stats, OOS_START, TARGET_VOL

OUT = Path(__file__).resolve().parent / "results"
COST = 0.0015
FN = {b[0]: b[2] for b in S.BOOK}
SKIP = {"2.51"}            # US jobs-report timing doesn't apply to NSE stocks


def _specs():
    sel = pd.read_csv(OUT / "daily_selected.csv", dtype={"strategy": str})
    out = [("BH", None, {})]
    for _, r in sel.iterrows():
        if r["strategy"] in SKIP:
            continue
        out.append((r["strategy"], FN[r["strategy"]], {**ast.literal_eval(r["cfg"]), "sides": "long"}))
    return out


def _chunk(syms):
    specs = _specs()
    sums = {}; cnt = None; ntr = {s[0]: 0 for s in specs}; wins = {s[0]: 0 for s in specs}
    for sym in syms:
        try:
            df = bhav.load(sym)
        except Exception:
            continue
        df = df[df["close"] > 0]
        if len(df) < 300 or not df["tradable"].any():
            continue
        d = S.prep(df)
        trad = df["tradable"].to_numpy(bool)
        o, c = d["o"], d["c"]
        r_on = np.nan_to_num(o / np.r_[np.nan, c[:-1]] - 1)
        r_id = np.nan_to_num(c / o - 1)
        r = pd.Series(c).pct_change()
        w = (TARGET_VOL / (r.rolling(60, min_periods=40).std() * np.sqrt(252))).clip(upper=5).shift(1).to_numpy()
        w = np.nan_to_num(w)
        tcount = pd.Series(trad.astype(float), index=df.index)
        cnt = tcount if cnt is None else cnt.add(tcount, fill_value=0)
        for sid, fn, cfg in specs:
            try:
                pos = np.ones(d["n"]) if sid == "BH" else np.clip(fn(d, cfg), 0, None)
            except Exception as e:
                print(f"ERR {sid} {sym}: {e!r}", flush=True); continue
            pos = np.where(trad, pos, 0.0)
            p1 = np.r_[0.0, pos[:-1]]; p2 = np.r_[0.0, 0.0, pos[:-2]]
            ret = p2 * r_on + p1 * r_id - COST * np.abs(p1 - p2)
            # trade outcomes
            ch = np.flatnonzero(np.diff(np.r_[0.0, p1, 0.0]) != 0)
            for a, b in zip(ch[:-1], ch[1:]):
                if p1[a] > 0:
                    ntr[sid] += 1; wins[sid] += ret[a:b].sum() > 0
            s = pd.Series(ret * w, index=df.index)
            sums[sid] = s if sid not in sums else sums[sid].add(s, fill_value=0)
    return sums, cnt, ntr, wins


if __name__ == "__main__":
    syms = bhav.list_symbols()
    chunks = [syms[i::12] for i in range(12)]
    t0 = time.time()
    print(f"{len(syms)} symbols, {len(_specs())} strategies …", flush=True)
    with Pool(12) as pool:
        parts = pool.map(_chunk, chunks)
    print(f"done in {time.time()-t0:.0f}s", flush=True)
    cnt = pd.concat([p[1] for p in parts if p[1] is not None], axis=1).sum(axis=1).sort_index()
    rows = []
    sel = pd.read_csv(OUT / "daily_selected.csv", dtype={"strategy": str}).set_index("strategy")
    for sid, _, cfg in _specs():
        tot = None
        for p in parts:
            if sid in p[0]:
                tot = p[0][sid] if tot is None else tot.add(p[0][sid], fill_value=0)
        port = (tot.reindex(cnt.index).fillna(0) / cnt.replace(0, np.nan)).dropna()
        port = port[port.index >= "2007-01-01"]
        a, b = stats(port[port.index < OOS_START]), stats(port[port.index >= OOS_START])
        k = TARGET_VOL / a["vol"] if a["vol"] else np.nan
        b10 = stats(port[port.index >= OOS_START] * k)
        n = sum(p[2][sid] for p in parts); wn = sum(p[3][sid] for p in parts)
        rows.append({"strategy": sid, "name": "Buy & hold (top-500, vol-scaled)" if sid == "BH" else S.NAMES[sid],
                     "IS_sharpe": a["sharpe"], "OOS_sharpe": b["sharpe"],
                     "OOS_cagr_10vol": b10["cagr"], "OOS_maxdd_10vol": b10["maxdd"],
                     "OOS_final_10L": 1e6 * float((1 + port[port.index >= OOS_START] * k).prod()),
                     "trades": n, "win_pct": 100 * wn / max(1, n), "cfg": str(cfg)})
    t = pd.DataFrame(rows)
    t.to_csv(OUT / "stocks_selected.csv", index=False)
    pd.set_option("display.width", 250); pd.set_option("display.max_colwidth", 45)
    print(t.round(3).to_string(index=False))
