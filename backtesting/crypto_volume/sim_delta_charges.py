import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
xs_taker long+short ($5k + $5k) under Delta Exchange India's real charges.

Delta (checked on delta.exchange/fees, 2026-10-20): futures taker 0.05%, maker
0.02%, +18% GST on fees -> taker 5.9 bps/side. Zero deposit/withdrawal fees.
Funding: Delta's own history (data/CRYPTO_PERPS_DELTA, % per 8h, accrued hourly),
Binance funding only where Delta has none.
Spread/depth: per-coin snapshot crypto/universe/delta_perps_liquidity.csv
(one moment, 2026-10-19 — a proxy, not history).

Scenarios (cost per side, per coin):
  A  as shown before      15 bps flat, Binance funding
  B  Delta fee only       5.9 bps — the floor (assumes zero spread)
  C  Delta realistic      5.9 + spread/2 + spread/2 x min(order / top depth, 4)
  D  Delta conservative   5.9 + full spread + 10 bps

Not included: income tax on profits, liquidation fees (we never get near
liquidation at 0.5x), the 50% "Scalper Offer" discount.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.sim_delta_charges
"""

from pathlib import Path

import numpy as np
import pandas as pd

from . import engine as E
from .robust_xs_taker import taker_ratio

ROOT = Path(__file__).parents[2]
OUT = Path(__file__).parent / "results"
DELTA_FUND = ROOT / "data" / "CRYPTO_PERPS_DELTA"
START, CAPITAL, L = "2024-01-01", 10_000, 0.5
TAKER_BPS = 5.0 * 1.18


def delta_funding(P: E.Panel, liq: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Per-bar funding (fraction of notional, long pays) from Delta where available."""
    fund = P.funding.copy()
    used = []
    for _, row in liq.iterrows():
        f = DELTA_FUND / row["delta"] / "funding_1h.parquet"
        if not f.exists() or row["binance"] not in fund.columns:
            continue
        s = pd.read_parquet(f).set_index("datetime")["rate_8h_pct"] / 100 / 8   # per hour
        per_bar = s.resample("1D").sum()
        idx = per_bar.index[per_bar.index >= START]
        fund.loc[fund.index.isin(idx), row["binance"]] = per_bar.reindex(
            fund.index[fund.index.isin(idx)]).fillna(0.0).values
        used.append(row["binance"])
    return fund, used


def main() -> None:
    liq = pd.read_csv(ROOT / "crypto" / "universe" / "delta_perps_liquidity.csv")
    liq = liq[liq["binance"].notna()].drop_duplicates("binance")
    allowed = set(liq.loc[liq["turnover_24h_usd"] >= 50e3, "binance"])
    P = E.Panel("1d", universe="pit")
    tr = taker_ratio(P, 14)
    tr.loc[:, [c for c in tr.columns if c not in allowed]] = np.nan
    w = L * E.xs_weights(P, tr, 3)

    # typical order size per coin, in $, at the starting capital
    dw = w.loc[START:].diff().abs()
    order_usd = (dw.where(dw > 1e-9).mean() * CAPITAL).fillna(0)
    li = liq.set_index("binance")
    spread = li["spread_bps"].reindex(w.columns)
    depth = li["top_depth_usd"].reindex(w.columns)
    impact = (spread / 2) * (order_usd / depth).clip(upper=4)
    cost_C = (TAKER_BPS + spread / 2 + impact).fillna(30.0)
    cost_D = (TAKER_BPS + spread + 10).fillna(40.0)

    traded = [c for c in w.columns if (w.loc[START:, c] != 0).any()]
    print(f"coins traded since {START}: {len(traded)}")
    print(f"per-side cost over traded coins (bps):  realistic C median {cost_C[traded].median():.1f}, "
          f"turnover-weighted {np.average(cost_C[traded], weights=order_usd[traded] + 1e-9):.1f}  |  "
          f"conservative D median {cost_D[traded].median():.1f}")
    worst = cost_C[traded].sort_values(ascending=False).head(6)
    print("most expensive coins (C):", ", ".join(f"{c.replace('USDT', '')} {v:.0f}" for c, v in worst.items()))

    binance_fund = P.funding
    dfund, used = delta_funding(P, liq)
    print(f"Delta funding history found for {len(set(used) & set(traded))} of {len(traded)} traded coins")

    rows = {}
    for name, cost, fund in (("A  as shown before (15 bps, Binance funding)", 15.0, binance_fund),
                             ("B  Delta fee only 5.9 bps (floor)", TAKER_BPS, dfund),
                             ("C  Delta realistic (fee+GST+spread+impact)", cost_C, dfund),
                             ("D  Delta conservative (fee+GST+spread+10)", cost_D, dfund)):
        P.funding = fund
        res = E.run(P, w, cost).resample("1D").sum().loc[START:]
        r = res["net"]
        eq = (1 + r).cumprod() * CAPITAL
        prev = eq.shift().fillna(CAPITAL)
        yrs = len(r) / 365
        y = (1 + r).groupby(r.index.year).prod() - 1
        rows[name] = {"end $": eq.iloc[-1], "CAGR": (eq.iloc[-1] / CAPITAL) ** (1 / yrs) - 1,
                      "maxDD": (eq / eq.cummax() - 1).min(),
                      "fees+slippage $": (res["cost"] * prev).sum(),
                      "funding $ (+paid/-received)": (res["funding"] * prev).sum(),
                      **{str(k): v for k, v in y.items()}}
    P.funding = binance_fund
    t = pd.DataFrame(rows).T
    money = {"end $", "fees+slippage $", "funding $ (+paid/-received)"}
    pd.set_option("display.width", 220)
    print("\n" + t.to_string(formatters={c: (lambda v: f"${v:,.0f}") if c in money else (lambda v: f"{v:.0%}")
                                         for c in t.columns}))
    t.to_csv(OUT / "sim_delta_charges.csv")


if __name__ == "__main__":
    main()
