import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Stress-test xs_taker: rank coins by taker-buy ratio over a lookback, long the
top bucket, short the bottom, inverse-vol inside each book.

Checks: per-coin bias (demeaned ratio), costs, bucket size, rebalance
frequency, listing age, long vs short legs, per-year Sharpe, BTC correlation.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.robust_xs_taker --tf 1d
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine as E

OUT = Path(__file__).parent / "results"


def taker_ratio(P: E.Panel, lb: int) -> pd.DataFrame:
    return (P.taker_buy.rolling(lb, min_periods=lb).sum()
            / P.volume.rolling(lb, min_periods=lb).sum())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1d")
    ap.add_argument("--universe", default="top100", choices=["top100", "pit"])
    args = ap.parse_args()
    P = E.Panel(args.tf, universe=args.universe)
    bpd = P.bpd
    btc = P.ret["BTCUSDT"].fillna(0).resample("1D").sum()
    rows, daily = [], {}

    def rec(label, w, cost=8.0, **info):
        res = E.run(P, w, cost)
        m = E.metrics(res)
        d = res["net"].resample("1D").sum()
        d = d.loc[res["exposure"].resample("1D").sum().gt(0).idxmax():]
        yearly = {f"S_{y}": E._sharpe(d[d.index.year == y]) for y in range(2020, 2027)}
        m.update(yearly, btc_corr=float(d.corr(btc.reindex(d.index))))
        rows.append({"variant": label, **info, **m})
        daily[label] = d
        print(f"{label:<44} S {m['sharpe']:5.2f} IS {m['sharpe_is']:5.2f} OOS {m['sharpe_oos']:5.2f} "
              f"25-26 {m['sharpe_2025_26']:5.2f} DD20 {m['maxdd_at20vol']:6.1%} "
              f"corrBTC {m['btc_corr']:5.2f}  yrs " +
              " ".join(f"{v:5.2f}" for v in yearly.values()), flush=True)

    for lbd in (3, 7, 14, 30):
        lb = lbd * bpd
        tr = taker_ratio(P, lb)
        rec(f"base lb={lbd}d", E.xs_weights(P, tr, bpd), lookback=lbd)

    lb = 7 * bpd
    tr = taker_ratio(P, lb)
    # per-coin bias: subtract each coin's own trailing 90-day mean ratio
    long_run = taker_ratio(P, 90 * bpd)
    rec("demeaned (vs own 90d) lb=7d", E.xs_weights(P, tr - long_run, bpd))
    rec("demeaned (vs own 180d) lb=7d", E.xs_weights(P, tr - taker_ratio(P, 180 * bpd), bpd))
    rec("static coin bias only (90d ratio)", E.xs_weights(P, long_run, bpd))

    for c in (15.0, 25.0):
        rec(f"cost {c:.0f} bps lb=7d", E.xs_weights(P, tr, bpd), cost=c)
    for q in (0.1, 0.3):
        rec(f"bucket q={q} lb=7d", E.xs_weights(P, tr, bpd, q=q))
    for rb in (3, 7):
        rec(f"rebalance every {rb}d lb=7d", E.xs_weights(P, tr, rb * bpd))

    # listing age >= 180 days
    first = P.close.apply(lambda s: s.first_valid_index())
    old = pd.DataFrame({p: P.index >= first[p] + pd.Timedelta(days=180)
                        for p in P.close.columns}, index=P.index)
    rec("only coins listed >=180d lb=7d", E.xs_weights(P, tr.where(old), bpd))
    # coins with full history since 2020 (closest we get to a survivorship check)
    early = [p for p in P.close.columns if first[p] <= pd.Timestamp("2020-06-30")]
    rec(f"only {len(early)} coins listed by mid-2020 lb=7d",
        E.xs_weights(P, tr[early].reindex(columns=P.close.columns), bpd, min_coins=8))

    w = E.xs_weights(P, tr, bpd)
    rec("long leg only lb=7d", w.clip(lower=0))
    rec("short leg only lb=7d", w.clip(upper=0))
    # beta-neutral-ish: long book minus BTC exposure is hard; report raw legs above

    OUT.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / f"xs_taker_robust_{args.tf}_{args.universe}.csv", index=False)
    pd.DataFrame(daily).to_parquet(OUT / f"xs_taker_robust_daily_{args.tf}_{args.universe}.parquet")


if __name__ == "__main__":
    main()
