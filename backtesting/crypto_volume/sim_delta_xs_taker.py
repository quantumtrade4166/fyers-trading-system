import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Realistic money sim of xs_taker on Delta Exchange India coins.

Signal  : 14d taker-buy ratio (Binance flow), PIT top-100 universe, restricted
          to coins Delta lists with >= $50k 24h turnover (today's snapshot,
          crypto/universe/delta_perps_liquidity.csv).
Trading : long top 20% / short bottom 20%, inverse-vol, rebalance every 3 days,
          15 bps per side (Delta taker 5 bps + 18% GST + spread/impact), funding.
Sizing  : unit weights = long book 1x, short book 1x of capital (gross 2x);
          scaled by L and compounded on real capital — no ex-post vol scaling.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.sim_delta_xs_taker
"""

from pathlib import Path

import numpy as np
import pandas as pd

from . import engine as E
from .robust_xs_taker import taker_ratio

ROOT = Path(__file__).parents[2]
OUT = Path(__file__).parent / "results"
START = "2024-01-01"


def main() -> None:
    liq = pd.read_csv(ROOT / "crypto" / "universe" / "delta_perps_liquidity.csv")
    allowed = set(liq.loc[liq["turnover_24h_usd"] >= 50e3, "binance"].dropna())
    P = E.Panel("1d", universe="pit")
    tr = taker_ratio(P, 14)
    tr.loc[:, [c for c in tr.columns if c not in allowed]] = np.nan
    w = E.xs_weights(P, tr, 3)
    res = E.run(P, w, cost_bps=15)
    r = res["net"].resample("1D").sum().loc[START:]
    n_books = (w.loc[START:] != 0).sum(axis=1)

    print(f"period {r.index[0]:%Y-%m-%d} -> {r.index[-1]:%Y-%m-%d}, "
          f"coins held per day (median) {int(n_books.median())}")
    print(f"unit book: ann. return {r.mean() * 365:.1%}, ann. vol {r.std() * np.sqrt(365):.1%}, "
          f"Sharpe {r.mean() / r.std() * np.sqrt(365):.2f}, "
          f"costs {res['cost'].loc[START:].sum():.1%}, funding {res['funding'].loc[START:].sum():.1%} of capital\n")

    rows = []
    for L in (0.5, 1.0, 1.5, 2.0, 3.0):
        eq = (1 + L * r).cumprod()
        dd = eq / eq.cummax() - 1
        yrs = len(r) / 365
        m = (1 + L * r).resample("ME").prod() - 1
        y = (1 + L * r).groupby(r.index.year).prod() - 1
        rows.append({"leverage(gross=2L)": L, "CAGR": eq.iloc[-1] ** (1 / yrs) - 1,
                     "maxDD": dd.min(), "CAGR/DD": (eq.iloc[-1] ** (1 / yrs) - 1) / -dd.min(),
                     "worst_month": m.min(), "pct_months_up": (m > 0).mean(),
                     **{f"ret_{k}": v for k, v in y.items()}})
    t = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(t.to_string(index=False, float_format=lambda v: f"{v:7.1%}" if abs(v) < 50 else f"{v:.1f}"))

    # size per coin vs Delta order-book depth today
    k = max(1, int(n_books.median() / 2))
    depth = liq.set_index("binance")["top_depth_usd"]
    held = sorted(set(w.loc[START:].columns[(w.loc[START:] != 0).any()]) & set(depth.index))
    print(f"\n~{k} coins per book. Notional per coin at L=1: capital/{k}. "
          f"Delta top-of-book depth over the coins traded: median ${depth[held].median():,.0f}, "
          f"10th pct ${depth[held].quantile(0.1):,.0f}")
    for cap in (10_000, 25_000, 50_000):
        print(f"  capital ${cap:>6,}: per coin at L=1 ≈ ${cap / k:,.0f}")

    OUT.mkdir(exist_ok=True)
    r.to_frame("net_unit").to_csv(OUT / "sim_delta_xs_taker_daily.csv")
    t.to_csv(OUT / "sim_delta_xs_taker_leverage.csv", index=False)


if __name__ == "__main__":
    main()
