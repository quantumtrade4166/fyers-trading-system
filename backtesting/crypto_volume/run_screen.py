import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Screen every volume strategy (and its no-volume twin) on the top-100 perps.

Output -> backtesting/crypto_volume/results/
  screen_{tf}_{tag}.csv       one row per (strategy, params, use_volume)
  daily_{tf}_{tag}.parquet    daily net P&L per config (MTM — for portfolio work)

Run:
  .venv\\Scripts\\python.exe -m backtesting.crypto_volume.run_screen --tf 1d
  .venv\\Scripts\\python.exe -m backtesting.crypto_volume.run_screen --tf 4h --only obv_trend xs_mom
"""

import argparse
import itertools
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine as E
from .strategies import TS

OUT = Path(__file__).parent / "results"
XS_LOOKBACK_DAYS = [7, 14, 30]


def _grid(spec: dict):
    keys = list(spec)
    for vals in itertools.product(*spec.values()):
        yield dict(zip(keys, vals))


def xs_configs(P: E.Panel):
    """(name, params, use_volume, weights-builder) for the cross-sectional family."""
    for lbd in XS_LOOKBACK_DAYS:
        lb = lbd * P.bpd
        ret = P.close.pct_change(lb, fill_method=None)
        vr = P.volume.rolling(lb, min_periods=lb).mean() / P.volume.rolling(4 * lb, min_periods=4 * lb).mean()
        tbr = P.taker_buy.rolling(lb, min_periods=lb).sum() / P.volume.rolling(lb, min_periods=lb).sum()
        prm = {"lookback_days": lbd}
        yield "xs_mom", prm, False, lambda ret=ret: E.xs_weights(P, ret, P.bpd)
        yield "xs_mom", prm, True, lambda ret=ret, vr=vr: E.xs_weights(
            P, ret, P.bpd, eligible_long=vr > 1, eligible_short=vr > 1)
        yield "xs_taker", prm, True, lambda tbr=tbr: E.xs_weights(P, tbr, P.bpd)
        yield "xs_volsurge", prm, True, lambda ret=ret, vr=vr: E.xs_weights(
            P, np.log(vr) * np.sign(ret), P.bpd)
        yield "xs_attention", prm, True, lambda vr=vr: E.xs_weights(P, -np.log(vr), P.bpd)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="1d", choices=["1h", "4h", "1d"])
    ap.add_argument("--cost-bps", type=float, default=8.0)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--universe", default="top100", choices=["top100", "pit"])
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    t0 = time.time()
    P = E.Panel(args.tf, universe=args.universe)
    print(f"{args.tf}: {len(P.dfs)} coins, {len(P.index)} bars "
          f"{P.index[0]:%Y-%m-%d} -> {P.index[-1]:%Y-%m-%d}  ({time.time() - t0:.0f}s)", flush=True)

    rows, daily = [], {}

    def record(name, prm, uv, w):
        res = E.run(P, w, args.cost_bps)
        m = E.metrics(res)
        key = f"{name}|{prm}|{'vol' if uv else 'base'}"
        rows.append({"strategy": name, "params": str(prm), "use_volume": uv, **m})
        daily[key] = res["net"].resample("1D").sum()
        print(f"  {name:<20} {str(prm):<36} {'VOL ' if uv else 'base'} "
              f"S {m['sharpe']:5.2f}  IS {m['sharpe_is']:5.2f}  OOS {m['sharpe_oos']:5.2f}  "
              f"25-26 {m['sharpe_2025_26']:5.2f}  DD {m['maxdd_at20vol']:6.1%}", flush=True)

    # benchmarks
    ones = pd.DataFrame(1.0, index=P.index, columns=P.close.columns)
    record("bench_ew_long", {}, False, E.ts_weights(P, ones))
    btc = ones.copy()
    btc.loc[:, [c for c in btc.columns if c != "BTCUSDT"]] = 0.0
    record("bench_btc_long", {}, False, E.ts_weights(P, btc))

    for name, (fn, spec) in TS.items():
        if args.only and name not in args.only:
            continue
        for prm in _grid(spec):
            for uv in (True, False):
                pos = pd.DataFrame({p: fn(df, use_volume=uv, **prm) for p, df in P.dfs.items()})
                record(name, prm, uv, E.ts_weights(P, pos))

    for name, prm, uv, build in xs_configs(P):
        if args.only and name not in args.only:
            continue
        record(name, prm, uv, build())

    tag = f"{args.tf}_{args.tag}" + ("_pit" if args.universe == "pit" else "")
    pd.DataFrame(rows).to_csv(OUT / f"screen_{tag}.csv", index=False)
    pd.DataFrame(daily).to_parquet(OUT / f"daily_{tag}.parquet")
    print(f"done {len(rows)} configs in {time.time() - t0:.0f}s -> {OUT / f'screen_{tag}.csv'}", flush=True)


if __name__ == "__main__":
    main()
