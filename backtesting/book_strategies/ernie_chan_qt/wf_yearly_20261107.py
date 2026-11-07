"""
wf_yearly_20261107.py — StatArb.MR-first10 with YEARLY re-calibration (walk-forward, no hindsight).

Every 1-Jan of year Y, for each pair, re-run the original calibration recipe on data up to 31-Dec of Y-1 ONLY:
  hedge ratio + half-life -> lookback = max(2*HL, 63) (skip pair that year if > 250 or beta <= 0),
  lots from beta, annual stop = 3x avg loss, entry/stop z picked by best Sharpe over {1.5,2,2.5} x {3,3.5,4},
  max half-life gate = 2x the in-sample half-life.
Then trade year Y with those settings (D settings: next-day open fill, 0.05%/side, 0.10%/month roll).
A position opened in year Y runs to its natural exit with the settings it was opened under;
the next year's settings only take new entries once that position is closed (+5-day cooldown).

Windows: "expanding" = all data from 2015 up to Y-1; "rolling3" = the last 3 years only.
Usage: python wf_yearly_20261107.py <daily data dir>
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant
import audit_rerun_20261106 as au
from portfolio_backtest import compute_signals, simulate, find_optimal_lots, sharpe_from_trades

OUT = au.OUT
FIRST_TEST, LAST_TEST = 2017, 2026
COST, ROLL = 0.0005, 0.001


def calibrate(ins, la, lb_):
    """Original recipe on in-sample closes `ins` (cols A, B). Returns params or None (pair skipped)."""
    if len(ins) < 400: return None
    pa, pb = ins.A.values, ins.B.values
    beta = OLS(pa, add_constant(pb)).fit().params[1]
    hl = au.hl_of(pa, pb)
    lookback = max(int(hl * 2), 63)
    if lookback > 250 or beta <= 0: return None
    nA, nB, _ = find_optimal_lots(beta, la, lb_)
    qa, qb = nA * la, nB * lb_
    if qa <= 0 or qb <= 0: return None
    z, hls = compute_signals(pa, pb, lookback)
    v1 = simulate(pa, pb, ins.index, z, hls, qa, qb, 2.0, 3.5, 9_999_999, hl * 2)
    losses = v1[v1.net_pnl < 0].net_pnl if not v1.empty else pd.Series(dtype=float)
    ann = max(int(abs(losses.mean()) * 3), 20_000) if len(losses) else 20_000
    cap = int((pa[-252:].mean() * qa + pb[-252:].mean() * qb) * au.SPAN)
    best = None
    for ez in (1.5, 2.0, 2.5):
        for sz in (3.0, 3.5, 4.0):
            t = simulate(pa, pb, ins.index, z, hls, qa, qb, ez, sz, ann, hl * 2)
            sh = sharpe_from_trades(t, cap)[0] if not t.empty else -9
            if best is None or sh > best[0]: best = (sh, ez, sz)
    return dict(qa=qa, qb=qb, lb=lookback, ez=best[1], sz=best[2], ann=ann, max_hl=hl * 2, is_sharpe=round(best[0], 2))


def run_pair(p, window):
    name, a, _, _, b, _, _, *_ = p
    la, lb_ = au.LOT_UNIT[name]
    A, B = au.load(a), au.load(b)
    df = pd.concat([A, B], axis=1, keys=["A", "B"]).dropna()
    ca, cb = df[("A", "close")], df[("B", "close")]
    oa, ob = df[("A", "open")], df[("B", "open")]
    closes = pd.DataFrame({"A": ca, "B": cb})
    daily_tot = pd.Series(0.0, index=ca.index)
    trades, cal_log = [], []
    free_from = pd.Timestamp(f"{FIRST_TEST}-01-01")
    for Y in range(FIRST_TEST, LAST_TEST + 1):
        y0, y1 = pd.Timestamp(f"{Y}-01-01"), pd.Timestamp(f"{Y}-12-31")
        lo = pd.Timestamp(f"{Y - 3}-01-01") if window == "rolling3" else pd.Timestamp("2000-01-01")
        ins = closes[(closes.index < y0) & (closes.index >= lo)]
        prm = calibrate(ins, la, lb_)
        cal_log.append(dict(pair=name, year=Y, **(prm or dict(skipped=True))))
        if prm is None or free_from > y1:
            continue
        z, hl = compute_signals(ca.values, cb.values, prm["lb"])
        mh = np.full(len(ca), prm["max_hl"])
        tr, daily, _ = au.sim(ca, cb, oa, ob, z, hl, mh, prm["qa"], prm["qb"], prm["ez"], prm["sz"], prm["ann"],
                              COST, ROLL, True, entry_from=max(y0, free_from), entry_until=y1)
        if tr.empty:
            continue
        # per-trade margin base for the return series (margin at entry)
        base = pd.Series(np.nan, index=ca.index)
        for _, t in tr.iterrows():
            base.loc[t.entry_date:t.exit_date] = au.SPAN * (ca[t.entry_date] * prm["qa"] + cb[t.entry_date] * prm["qb"])
        r = (daily / base.ffill().bfill()).fillna(0.0)
        daily_tot = daily_tot.add(r, fill_value=0.0)
        tr = tr.assign(pair=name, cal_year=Y, lb=prm["lb"], ez=prm["ez"], sz=prm["sz"], qa=prm["qa"], qb=prm["qb"],
                       entry_margin=[au.SPAN * (ca[d] * prm["qa"] + cb[d] * prm["qb"]) for d in tr.entry_date])
        trades.append(tr)
        last_exit = tr.exit_date.max()
        k = int(np.searchsorted(ca.index, last_exit))
        free_from = ca.index[min(k + au.COOLDOWN, len(ca) - 1)] if k + au.COOLDOWN < len(ca) else pd.Timestamp("2100-01-01")
    return daily_tot, (pd.concat(trades) if trades else pd.DataFrame()), cal_log


def go(window, only_tradeable):
    pairs = [p for p in au.PAIRS if not (only_tradeable and (p[1] in au.NO_FUTURES or p[4] in au.NO_FUTURES))]
    R, T, C = {}, [], []
    for p in pairs:
        r, t, c = run_pair(p, window); R[p[0]] = r; C += c
        if not t.empty: T.append(t)
    r = pd.DataFrame(R).fillna(0.0).sum(axis=1) / len(pairs)
    r = r[r.index >= pd.Timestamp(f"{FIRST_TEST}-01-01")]
    return r, (pd.concat(T) if T else pd.DataFrame()), pd.DataFrame(C)


if __name__ == "__main__":
    pd.set_option("display.width", 250)
    rows, yearly = [], {}
    for window in ("expanding", "rolling3"):
        for tradeable in (False, True):
            label = f"WFY_{window}_{'tradeable' if tradeable else 'all'}"
            r, t, c = go(window, tradeable)
            r.rename("ret").to_csv(OUT / f"returns_{label}.csv")
            t.to_csv(OUT / f"trades_{label}.csv", index=False); c.to_csv(OUT / f"calib_{label}.csv", index=False)
            for lab, rr in ((label, r), (label + "_2021on", r[r.index >= "2021-01-01"])):
                s = au.stats_from_returns(rr, lab); s50 = au.stats_from_returns(rr * 0.5, lab)
                n = t[t.entry_date >= rr.index[0]] if not t.empty else t
                rows.append(dict(run=lab, CAGR=s["CAGR"], MaxDD=s["MaxDD"], Sharpe=s["Sharpe"], worst_year=s["worst_year"],
                                 trades=len(n), win_pct=round((n.net_pnl > 0).mean() * 100, 1) if len(n) else 0,
                                 **{"CAGR@50%": s50["CAGR"], "MaxDD@50%": s50["MaxDD"]}))
            yearly[label] = (s_full := au.stats_from_returns(r, label))["yearly"].mul(100).round(1)
            print(f"done {label}: {len(t)} trades")
            if window == "expanding" and tradeable:
                piv = c.pivot(index="pair", columns="year", values="lb") if "lb" in c else None
                print("\nLookback chosen each year (blank = pair skipped that year):\n" + piv.to_string(na_rep="-") + "\n")
    summ = pd.DataFrame(rows).set_index("run")
    print("\n" + summ.to_string())
    print("\nYEARLY RETURN % (100% margin use)\n" + pd.DataFrame(yearly).to_string())
    summ.to_csv(OUT / "summary_wf_yearly.csv"); pd.DataFrame(yearly).to_csv(OUT / "yearly_wf_yearly.csv")
