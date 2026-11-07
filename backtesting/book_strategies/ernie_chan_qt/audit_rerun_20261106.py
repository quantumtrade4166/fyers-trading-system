"""
audit_rerun_20261106.py — full audit rerun of StatArb.MR-first10 (2015 -> 4-Nov-2026)

Same signal + exit rules as portfolio_backtest.py. Corrections are added one at a time:
  A  clean data (NSE days only), original params, fill at the signal close, 0.03% cost
  B  A + fill at NEXT day's open (you can't trade at a close you haven't seen yet)
  C  B + no look-ahead in the half-life gate (max_hl from data up to that day only)
  D  C + realistic costs: 0.05%/side + futures roll 0.10% of notional per month held
  E  D, only pairs that have futures on both legs (drops OBEROI/BRIGADE, SRF/DEEPAKNTR)
  WF walk-forward: params calibrated on 2015-2020 only, tested 2021 -> 2026 (D settings)

CAGR (capital-based, compounding): each pair gets 10% of capital as its margin
(SPAN = 15% of both legs' notional) -> returns are scale-free, unlike fixed share counts.
Also shown at 50% margin use (half-size positions, a cash buffer for MTM swings).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
from pathlib import Path
import numpy as np, pandas as pd
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parents[2]))
from portfolio_backtest import compute_signals, simulate, load_pair, find_optimal_lots, sharpe_from_trades, PAIRS as BT_PAIRS
from deployment.pair_config import PAIRS

DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parents[2] / "Nifty 500 Daily Data"
END = pd.Timestamp("2026-11-04")
OUT = HERE / "results" / "audit_20261106"; OUT.mkdir(parents=True, exist_ok=True)
EXIT_Z, COOLDOWN, SPAN = 0.5, 5, 0.15
NO_FUTURES = {"BRIGADE", "DEEPAKNTR"}
LOT_UNIT = {r[0]: (r[3], r[6]) for r in BT_PAIRS}


def load(sym):
    d = pd.read_parquet(DATA / f"{sym}.parquet"); d.index = pd.to_datetime(d.index)
    d = d[(d.volume > 0) & (d.index <= END) & (d.index >= "2015-01-01")].sort_index()
    return d[["open", "close"]]


def hl_of(pa, pb):
    beta = OLS(pa, add_constant(pb)).fit().params[1]
    sp = pa - beta * pb
    phi = OLS(np.diff(sp), add_constant(sp[:-1])).fit().params[1]
    return -np.log(2) / np.log(1 + phi) if phi < 0 else 999


def expanding_max_hl(pa, pb, step=21, warm=252):
    out = np.full(len(pa), np.inf)          # gate open until enough history (backtest has no gate either)
    for t in range(warm, len(pa), step):
        out[t:t + step] = hl_of(pa[:t], pb[:t]) * 2
    return out


def sim(close_a, close_b, exec_a, exec_b, z, hl, max_hl, qa, qb, ez, sz, ann,
        cost=0.0003, roll=0.0, next_open=False, entry_from=None, entry_until=None):
    """simulate() clone with fill price choice, roll cost and a daily MTM series.
    Decisions use the close of day t. Fill = close t, or open t+1 when next_open."""
    n = len(close_a); pos = 0; ea = eb = 0.0; e_i = 0; yr = 0.0; cur = None; cd = 0
    daily = np.zeros(n); margin = np.zeros(n); trades = []; pending = None
    dates = close_a.index; ca, cb = close_a.values, close_b.values
    oa, ob = exec_a.values, exec_b.values
    prev_val = 0.0
    def notional(a, b): return a * qa + b * qb
    for t in range(n):
        if dates[t].year != cur: cur = dates[t].year; yr = 0.0
        # 1) execute yesterday's order at today's open
        if pending is not None:
            kind, d = pending; pending = None
            fa, fb = oa[t], ob[t]
            if kind == "open":
                pos, ea, eb, e_i = d, fa, fb, t
                prev_val = -cost * notional(fa, fb)          # entry cost booked at fill
                daily[t] += prev_val
            else:
                g = ((fa - ea) * qa - (fb - eb) * qb) * pos
                months = (t - e_i) / 21.0
                net = g - cost * (notional(ea, eb) + notional(fa, fb)) - roll * months * notional(ea, eb)
                # daily series: realise the gap between last close MTM and this fill
                daily[t] += net - prev_val_net(prev_val)
                yr += net; trades.append(dict(entry_date=dates[e_i], exit_date=dates[t], hold_days=(dates[t] - dates[e_i]).days,
                                              direction="LongA" if pos == 1 else "ShortA", net_pnl=net, exit_reason=d))
                pos = 0; cd = t + COOLDOWN; prev_val = 0.0
        if entry_until is not None and pos == 0 and pending is None and dates[t] > entry_until:
            break                                     # entry window over and flat: this run is done
        if np.isnan(z[t]):
            continue
        # 2) mark open position to today's close
        if pos != 0:
            g = ((ca[t] - ea) * qa - (cb[t] - eb) * qb) * pos
            val = g - cost * notional(ea, eb) - roll * ((t - e_i) / 21.0) * notional(ea, eb)
            daily[t] += val - prev_val_net(prev_val); prev_val = val
            margin[t] = SPAN * notional(ca[t], cb[t])
            mtm = g
            er = None
            if pos == 1 and z[t] >= -EXIT_Z: er = "z_exit"
            if pos == -1 and z[t] <= EXIT_Z: er = "z_exit"
            if abs(z[t]) >= sz: er = "z_stop"
            if yr + mtm < -ann: er = "annual_stop"
            if er:
                if next_open and t + 1 < n:
                    pending = ("close", er)
                else:
                    net = val - cost * notional(ca[t], cb[t])
                    daily[t] += net - val
                    yr += net; trades.append(dict(entry_date=dates[e_i], exit_date=dates[t], hold_days=(dates[t] - dates[e_i]).days,
                                                  direction="LongA" if pos == 1 else "ShortA", net_pnl=net, exit_reason=er))
                    pos = 0; cd = t + COOLDOWN; prev_val = 0.0
            continue
        if pending is not None or t < cd or yr < -ann or hl[t] > max_hl[t]:
            continue
        if (entry_from is not None and dates[t] < entry_from) or (entry_until is not None and dates[t] > entry_until):
            continue
        d = 1 if z[t] < -ez else (-1 if z[t] > ez else 0)
        if d:
            if next_open:
                if t + 1 < n: pending = ("open", d)
            else:
                pos, ea, eb, e_i = d, ca[t], cb[t], t
                prev_val = -cost * notional(ca[t], cb[t]); daily[t] += prev_val
    if pos != 0:      # open at the end: already marked daily; log it
        trades.append(dict(entry_date=dates[e_i], exit_date=dates[-1], hold_days=(dates[-1] - dates[e_i]).days,
                           direction="LongA" if pos == 1 else "ShortA", net_pnl=prev_val, exit_reason="open_at_end"))
    return pd.DataFrame(trades), pd.Series(daily, index=dates), pd.Series(margin, index=dates)


def prev_val_net(v):
    return v


def stats_from_returns(r, label):
    r = r.fillna(0.0)
    nav = (1 + r).cumprod()
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    cagr = nav.iloc[-1] ** (1 / yrs) - 1
    dd = (nav / nav.cummax() - 1).min()
    sh = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    return dict(run=label, CAGR=round(cagr * 100, 1), MaxDD=round(dd * 100, 1), Sharpe=round(sh, 2),
                worst_year=round(yearly.min() * 100, 1), yearly=yearly)


def portfolio_returns(per_pair):
    """Equal 10% capital slice per pair, slice = margin at entry. Daily slice return = MTM change / entry margin."""
    rets = {}
    for name, (tr, daily, margin, df, qa, qb) in per_pair.items():
        base = pd.Series(np.nan, index=daily.index)
        for _, t in tr.iterrows():
            m = SPAN * (df.loc[t.entry_date, "A"] * qa + df.loc[t.entry_date, "B"] * qb)
            base.loc[t.entry_date:t.exit_date] = m
        base = base.ffill().bfill()
        rets[name] = (daily / base).fillna(0.0)
    R = pd.DataFrame(rets).fillna(0.0)
    return R.sum(axis=1) / len(per_pair)   # equal slice per pair traded in this run; idle slice = cash (0%)


def run(label, *, next_open=False, la_free=False, cost=0.0003, roll=0.0, only_tradeable=False,
        params=None, start=None):
    per = {}; rupees = 0.0; ntr = 0
    for p in PAIRS:
        name, a, qa, _, b, qb, _, lb, ez, sz, ann = p
        if only_tradeable and (a in NO_FUTURES or b in NO_FUTURES):
            continue
        if params is not None:
            if name not in params: continue
            qa, qb, lb, ez, sz, ann = params[name]
        A, B = load(a), load(b)
        df = pd.concat([A, B], axis=1, keys=[a, b]).dropna()
        ca, cb = df[(a, "close")], df[(b, "close")]
        oa, ob = (df[(a, "open")], df[(b, "open")]) if next_open else (ca, cb)
        z, hl = compute_signals(ca.values, cb.values, lb)
        mh = expanding_max_hl(ca.values, cb.values) if la_free else np.full(len(ca), hl_of(ca.values, cb.values) * 2)
        if start is not None:
            k = int(np.searchsorted(ca.index, pd.Timestamp(start)))
            ca, cb, oa, ob, z, hl, mh = ca.iloc[k:], cb.iloc[k:], oa.iloc[k:], ob.iloc[k:], z[k:], hl[k:], mh[k:]
        tr, daily, margin = sim(ca, cb, oa, ob, z, hl, mh, qa, qb, ez, sz, ann, cost, roll, next_open)
        dfc = pd.DataFrame({"A": ca, "B": cb})
        per[name] = (tr, daily, margin, dfc, qa, qb)
        rupees += daily.sum(); ntr += len(tr)
        tr.assign(pair=name, run=label).to_csv(OUT / f"trades_{label}.csv", mode="a", index=False,
                                                header=not (OUT / f"trades_{label}.csv").exists())
    r = portfolio_returns(per)
    if start is not None: r = r[r.index >= pd.Timestamp(start)]
    r.rename("ret").to_csv(OUT / f"returns_{label}.csv")
    s = stats_from_returns(r, label); s["rupees_L"] = round(rupees / 1e5, 1); s["trades"] = ntr
    s50 = stats_from_returns(r * 0.5, label); s["CAGR@50%"] = s50["CAGR"]; s["MaxDD@50%"] = s50["MaxDD"]
    pair_pnl = {n: round(v[1].sum() / 1e5, 1) for n, v in per.items()}
    return s, pair_pnl, per


def calibrate_in_sample(cut="2020-12-31"):
    """Repeat the original calibration recipe using 2015-2020 data only."""
    params = {}
    for p in PAIRS:
        name, a, _, _, b, _, _, *_ = p
        la, lb_ = LOT_UNIT[name]
        A, B = load(a), load(b)
        df = pd.concat([A.close, B.close], axis=1, keys=["A", "B"]).dropna()
        ins = df[df.index <= cut]
        if len(ins) < 500: continue
        pa, pb = ins.A.values, ins.B.values
        beta = OLS(pa, add_constant(pb)).fit().params[1]
        hl = hl_of(pa, pb); max_hl = hl * 2
        lookback = max(int(hl * 2), 63)
        if lookback > 250: continue     # no usable mean reversion in-sample
        nA, nB, _ = find_optimal_lots(beta, la, lb_)
        qa, qb = nA * la, nB * lb_
        if qb <= 0 or beta <= 0: continue
        z, hls = compute_signals(pa, pb, lookback)
        v1 = simulate(pa, pb, ins.index, z, hls, qa, qb, 2.0, 3.5, 9_999_999, max_hl)
        losses = v1[v1.net_pnl < 0].net_pnl if not v1.empty else pd.Series(dtype=float)
        ann = max(int(abs(losses.mean()) * 3), 20_000) if len(losses) else 20_000
        cap = int((pa[-252:].mean() * qa + pb[-252:].mean() * qb) * SPAN)
        best = None
        for ez in (1.5, 2.0, 2.5):
            for sz in (3.0, 3.5, 4.0):
                t = simulate(pa, pb, ins.index, z, hls, qa, qb, ez, sz, ann, max_hl)
                sh = sharpe_from_trades(t, cap)[0] if not t.empty else -9
                if best is None or sh > best[0]: best = (sh, ez, sz)
        params[name] = (qa, qb, lookback, best[1], best[2], ann)
        print(f"   WF calib {name:22s} LB={lookback:3d} EZ={best[1]} SZ={best[2]} ann={ann:,}  (live: LB={p[7]} EZ={p[8]} SZ={p[9]})  in-sample Sharpe={best[0]:.2f}")
    return params


if __name__ == "__main__":
    for f in OUT.glob("trades_*.csv"): f.unlink()
    pd.set_option("display.width", 250)
    # 0) reproduce the original exactly (original data files + original simulate)
    tot = 0
    for r in BT_PAIRS:
        a, b = load_pair(r[0], r[1], r[2], r[4], r[5])
        df = pd.DataFrame({"A": a, "B": b}).dropna(); df = df[df.index >= "2015-01-01"]
        pa, pb = df.A.values, df.B.values
        cfg = next(p for p in PAIRS if p[0] == r[0])
        z, hl = compute_signals(pa, pb, cfg[7])
        t = simulate(pa, pb, df.index, z, hl, cfg[2], cfg[5], cfg[8], cfg[9], cfg[10], hl_of(pa, pb) * 2)
        tot += t.net_pnl.sum()
    print(f"0) ORIGINAL reproduced: Rs {tot/1e5:.2f}L  (original report: Rs 486.57L)\n")

    # validation: sim() == simulate() when fills are at the close
    p = PAIRS[3]; A, B = load(p[1]), load(p[4]); df = pd.concat([A.close, B.close], axis=1, keys=["A", "B"]).dropna()
    z, hl = compute_signals(df.A.values, df.B.values, p[7]); mh = hl_of(df.A.values, df.B.values) * 2
    t0 = simulate(df.A.values, df.B.values, df.index, z, hl, p[2], p[5], p[8], p[9], p[10], mh)
    t1, d1, _ = sim(df.A, df.B, df.A, df.B, z, hl, np.full(len(df), mh), p[2], p[5], p[8], p[9], p[10])
    print(f"   check sim()==simulate(): {t0.net_pnl.sum():,.0f} vs {t1.net_pnl.sum():,.0f}  daily-sum {d1.sum():,.0f}\n")

    rows, pairs_tbl, keep = [], {}, {}
    cfgs = [("A_clean", {}), ("B_next_open", dict(next_open=True)),
            ("C_no_lookahead", dict(next_open=True, la_free=True)),
            ("D_real_costs", dict(next_open=True, la_free=True, cost=0.0005, roll=0.001)),
            ("E_tradeable_only", dict(next_open=True, la_free=True, cost=0.0005, roll=0.001, only_tradeable=True)),
            ("D_2021on_inSampleParams", dict(next_open=True, la_free=True, cost=0.0005, roll=0.001, start="2021-01-01"))]
    for label, kw in cfgs:
        s, pp, per = run(label, **kw); rows.append(s); pairs_tbl[label] = pp; keep[label] = per
        print(f"   done {label}")
    print("\n   Walk-forward calibration (2015-2020 only):")
    wf = calibrate_in_sample()
    for label, kw in (("WF_2021on_all", {}), ("WF_2021on_tradeable", dict(only_tradeable=True))):
        s, pp, _ = run(label, next_open=True, la_free=True, cost=0.0005, roll=0.001, params=wf, start="2021-01-01", **kw)
        rows.append(s); pairs_tbl[label] = pp

    summ = pd.DataFrame([{k: v for k, v in r.items() if k != "yearly"} for r in rows]).set_index("run")
    print("\n" + summ.to_string())
    yearly = pd.DataFrame({r["run"]: (r["yearly"] * 100).round(1) for r in rows})
    print("\nYEARLY RETURN % (100% margin use)\n" + yearly.to_string())
    print("\nRUPEE P&L PER PAIR (fixed share counts, Rs lakh)\n" + pd.DataFrame(pairs_tbl).to_string())
    summ.to_csv(OUT / "summary.csv"); yearly.to_csv(OUT / "yearly.csv"); pd.DataFrame(pairs_tbl).to_csv(OUT / "pair_pnl.csv")

    # Original "30.7% on Rs23.75L" feasibility: fixed shares, capital = 2015 peak margin, profits reinvested
    per = keep["A_clean"]
    pnl = sum(v[1] for v in per.values()); mar = sum(v[2].reindex(pnl.index, fill_value=0) for v in per.values())
    eq = 23.75e5 + pnl.cumsum()
    short = (mar - eq)
    print(f"\nOriginal 30.7% method check: margin needed > capital on {(short > 0).sum()} days; worst shortfall Rs {short.max()/1e5:.1f}L on {short.idxmax().date()}")
    print(f"   equity low = Rs {eq.min()/1e5:.1f}L;  peak margin needed = Rs {mar.max()/1e5:.1f}L")
