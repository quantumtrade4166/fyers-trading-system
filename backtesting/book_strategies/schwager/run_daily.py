import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

r"""
Book #2 (Schwager) — every daily strategy on a 15-market futures-style
portfolio.

    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.schwager.run_daily

Execution : position decided at the close, traded at the NEXT open
Costs     : per market (data.MARKETS), on every unit changed; BTC also pays
            10%/yr funding on longs
Sizing    : each market scaled to 10% annual volatility (60-day, lagged) —
            the book's "market volatility adjustment"; portfolio = average
            of the markets trading that day (business-day calendar, BTC
            weekends rolled into Monday)
Honesty   : each strategy's settings CHOSEN on 2003-2016 (portfolio Sharpe)
            and REPORTED on 2017-2026, which those settings never saw
"""

import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.book_strategies.schwager import data as D
from backtesting.book_strategies.schwager import strategies as S

OUT = Path(__file__).resolve().parent / "results"
TARGET_VOL = 0.10
OOS_START = "2017-01-01"


def _ann(m):
    return 365 if m == "BTC" else 252


def market_returns(name: str, pos: np.ndarray, df: pd.DataFrame) -> pd.DataFrame:
    """Daily strategy return (unit position) + trade list for one market."""
    _, cost, fund, _ = D.MARKETS[name]
    o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
    pc = np.r_[np.nan, c[:-1]]
    r_on = o / pc - 1                         # close(t-1) -> open(t)
    r_id = c / o - 1                          # open(t) -> close(t)
    p1 = np.r_[0.0, pos[:-1]]                 # executed at today's open
    p2 = np.r_[0.0, 0.0, pos[:-2]]            # held over last night
    ret = p2 * np.nan_to_num(r_on) + p1 * np.nan_to_num(r_id) - cost * np.abs(p1 - p2)
    if fund:
        ret -= fund / 365 * np.clip(p1, 0, None)
    return pd.Series(ret, index=df.index), p1


def trades_from(p1: np.ndarray, ret: np.ndarray):
    """Trade returns: runs of constant non-zero executed position."""
    out = []
    cur, acc = 0.0, 0.0
    for x, r in zip(p1, ret):
        if x != cur:
            if cur != 0:
                out.append(acc)
            cur, acc = x, 0.0
        if x != 0:
            acc += r
    if cur != 0:
        out.append(acc)
    return out


def vol_weight(df: pd.DataFrame, name: str) -> pd.Series:
    r = df["close"].pct_change()
    v = r.rolling(60, min_periods=40).std() * np.sqrt(_ann(name))
    return (TARGET_VOL / v).clip(upper=10).shift(1)


def to_bdays(s: pd.Series) -> pd.Series:
    idx = s.index
    shift = np.where(idx.dayofweek == 5, 2, np.where(idx.dayofweek == 6, 1, 0))
    s = s.copy(); s.index = idx + pd.to_timedelta(shift, unit="D")
    return s.groupby(level=0).sum()


def _work(args):
    """All configs of all strategies for ONE market."""
    name, specs = args
    df = D.load(name)
    d = S.prep(df)
    w = vol_weight(df, name)
    res = []
    for sid, ci, fn, cfg in specs:
        try:
            pos = fn(d, cfg)
        except Exception as e:
            print(f"  ERR {sid} {cfg} {name}: {e!r}", flush=True)
            continue
        ret, p1 = market_returns(name, pos, df)
        scaled = to_bdays(ret * w)
        tr = trades_from(p1, ret.to_numpy())
        tr_oos = trades_from(p1[df.index >= OOS_START], ret[df.index >= OOS_START].to_numpy())
        res.append((sid, ci, name, scaled, ret, len(tr), tr_oos))
    return res


def stats(r: pd.Series, ann=252) -> dict:
    r = r.dropna()
    if len(r) < 50 or r.std() == 0:
        return {"sharpe": np.nan, "cagr": np.nan, "maxdd": np.nan, "vol": np.nan}
    eq = (1 + r).cumprod()
    yrs = len(r) / ann
    return {"sharpe": r.mean() / r.std() * np.sqrt(ann),
            "cagr": eq.iloc[-1] ** (1 / yrs) - 1,
            "maxdd": (eq / eq.cummax() - 1).min(),
            "vol": r.std() * np.sqrt(ann)}


def specs_all():
    out = []
    for sid, name, fn, cfgs in S.BOOK:
        for i, cfg in enumerate(cfgs):
            out.append((sid, i, fn, cfg))
    return out


def run(markets=None, workers=10):
    markets = markets or D.available()
    specs = specs_all()
    print(f"{len(specs)} configs x {len(markets)} markets …", flush=True)
    t0 = time.time()
    with Pool(min(workers, len(markets))) as pool:
        parts = pool.map(_work, [(m, specs) for m in markets])
    print(f"done in {time.time()-t0:.0f}s", flush=True)
    by_cfg = {}
    for part in parts:
        for sid, ci, m, scaled, ret, ntr, tr_oos in part:
            by_cfg.setdefault((sid, ci), {})[m] = (scaled, ret, ntr, tr_oos)
    return markets, by_cfg


def evaluate(markets, by_cfg, bh=None):
    rows, permk = [], []
    cfg_of = {(sid, i): cfg for sid, _, _, cfgs in S.BOOK for i, cfg in enumerate(cfgs)}
    for (sid, ci), mk in by_cfg.items():
        P = pd.DataFrame({m: v[0] for m, v in mk.items()}).sort_index()
        port = P.mean(axis=1, skipna=True)
        IS, OOS = port[port.index < OOS_START], port[port.index >= OOS_START]
        si, so = stats(IS), stats(OOS)
        tr = [x for v in mk.values() for x in v[3]]
        wins = [x for x in tr if x > 0]; loss = [x for x in tr if x <= 0]
        row = {"strategy": sid, "cfg_id": ci, "cfg": str(cfg_of[(sid, ci)]),
               "IS_sharpe": si["sharpe"], "IS_cagr": si["cagr"], "IS_maxdd": si["maxdd"],
               "OOS_sharpe": so["sharpe"], "OOS_cagr": so["cagr"], "OOS_maxdd": so["maxdd"],
               "OOS_trades": len(tr), "OOS_win_pct": 100 * len(wins) / max(1, len(tr)),
               "OOS_pf": sum(wins) / abs(sum(loss)) if loss and sum(loss) else np.nan}
        # whole portfolio levered to 10% vol with a scale fixed on IS data only
        if si["vol"] and si["vol"] == si["vol"]:
            s10 = stats(OOS * min(TARGET_VOL / si["vol"], 20))
            row["OOS_cagr_10vol"], row["OOS_maxdd_10vol"] = s10["cagr"], s10["maxdd"]
            row["OOS_final_10L"] = 1e6 * float((1 + OOS * min(TARGET_VOL / si["vol"], 20)).prod())
        if bh is not None:
            row["OOS_corr_BH"] = OOS.corr(bh.reindex(OOS.index))
        for g in sorted({v[3] for v in D.MARKETS.values()}):
            cols = [m for m in P.columns if D.MARKETS[m][3] == g]
            gp = P[cols].mean(axis=1, skipna=True)
            row[f"OOS_sh_{g}"] = stats(gp[gp.index >= OOS_START])["sharpe"]
        rows.append(row)
        for m in P.columns:
            s = P[m]
            permk.append({"strategy": sid, "cfg_id": ci, "market": m,
                          "IS_sharpe": stats(s[s.index < OOS_START])["sharpe"],
                          "OOS_sharpe": stats(s[s.index >= OOS_START])["sharpe"]})
    return pd.DataFrame(rows), pd.DataFrame(permk)


def benchmark(markets):
    """Buy-and-hold, same vol scaling, same portfolio rule."""
    cols = {}
    for m in markets:
        df = D.load(m)
        ret, _ = market_returns(m, np.ones(len(df)), df)
        cols[m] = to_bdays(ret * vol_weight(df, m))
    port = pd.DataFrame(cols).sort_index().mean(axis=1, skipna=True)
    return stats(port[port.index < OOS_START]), stats(port[port.index >= OOS_START]), port


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    markets, by_cfg = run()
    bi, bo, bport = benchmark(markets)
    allc, permk = evaluate(markets, by_cfg, bport)
    allc.to_csv(OUT / "daily_all_configs.csv", index=False)
    permk.to_csv(OUT / "daily_per_market.csv", index=False)
    order = {b[0]: i for i, b in enumerate(S.BOOK)}
    sel = []
    for sid, g in allc.groupby("strategy"):
        p = g.sort_values("IS_sharpe", ascending=False).iloc[0].copy()
        p["cfgs_OOS_pos"] = f"{int((g['OOS_sharpe'] > 0).sum())}/{len(g)}"
        p["OOS_sharpe_median_cfg"] = g["OOS_sharpe"].median()
        sel.append(p)
    sel = pd.DataFrame(sel)
    sel.insert(1, "name", sel["strategy"].map(S.NAMES))
    sel = sel.assign(_o=sel["strategy"].map(order)).sort_values("_o").drop(columns="_o")
    sel.to_csv(OUT / "daily_selected.csv", index=False)
    bport.to_frame("buy_hold").to_csv(OUT / "benchmark_buyhold.csv")
    b10 = stats(bport[bport.index >= OOS_START] * TARGET_VOL / bi["vol"])
    pd.set_option("display.width", 320); pd.set_option("display.max_columns", 40)
    pd.set_option("display.max_colwidth", 50)
    print(f"\nMarkets: {markets}")
    print(f"Buy & hold (vol-scaled) portfolio: IS Sharpe {bi['sharpe']:.2f} | OOS Sharpe {bo['sharpe']:.2f}, "
          f"CAGR {bo['cagr']:.1%}, maxDD {bo['maxdd']:.1%} | at 10% vol: CAGR {b10['cagr']:.1%}, maxDD {b10['maxdd']:.1%}")
    cols = ["strategy", "name", "IS_sharpe", "OOS_sharpe", "OOS_cagr_10vol", "OOS_maxdd_10vol", "OOS_corr_BH",
            "OOS_trades", "OOS_win_pct", "OOS_pf", "cfgs_OOS_pos", "OOS_sharpe_median_cfg"] + [c for c in sel.columns if c.startswith("OOS_sh_")] + ["cfg"]
    print(sel[cols].round(3).to_string(index=False))
