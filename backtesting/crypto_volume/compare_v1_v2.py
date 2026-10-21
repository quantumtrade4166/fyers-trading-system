import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Taker Flow L/S — v1 (running in paper) vs v2 (staggered + beta-neutral), $10,000.

  A. Delta-tradable coins, 2024-01 -> now, Delta realistic charges (fee + 18% GST +
     spread/2 + depth impact, Delta funding) — the setup behind the $45.6k result
  B. Point-in-time top-100 Binance perps, 2020-03 -> now, 15 bps/side + Binance funding

Both versions: $5k long + $5k short (0.5 per book), 14d taker-buy ratio, top/bottom 20%.
v2 = three sub-books rebalancing on days 0/1/2 of the 3-day cycle (one netted book)
     + long/short books scaled to equal market beta (gross exposure unchanged).

Output: results/compare_v1_v2.png + printed tables.
Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.compare_v1_v2
"""

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import engine as E
from .improve_xs_taker import betas, beta_neutral
from .plot_equity import SURFACE, INK, INK2, GRID, _style, _usd
from .robust_xs_taker import taker_ratio
from .sim_delta_charges import ROOT, TAKER_BPS, delta_funding

OUT = Path(__file__).parent / "results"
CAP, L = 10_000, 0.5
C1, C2, CG = "#2a78d6", "#eb6834", "#8a8984"


def weights(P, score, v2: bool, B=None):
    if not v2:
        return L * E.xs_weights(P, score, 3)
    w = sum(E.xs_weights(P, score, 3, offset=k) for k in range(3)) / 3
    return L * beta_neutral(w, B)


def summary(r: pd.Series) -> dict:
    eq = CAP * (1 + r).cumprod()
    yrs = len(r) / 365
    m = (1 + r).resample("ME").prod() - 1
    return {"end": eq.iloc[-1], "CAGR": (eq.iloc[-1] / CAP) ** (1 / yrs) - 1,
            "maxDD": (eq / eq.cummax() - 1).min(),
            "Sharpe": r.mean() / r.std() * np.sqrt(365), "months_up": (m > 0).mean(),
            "n_months": len(m), "worst_month": m.min(), "best_month": m.max(),
            "years": ((1 + r).groupby(r.index.year).prod() - 1).to_dict(), "eq": eq}


def main() -> None:
    P = E.Panel("1d", universe="pit")
    B = betas(P)
    tr = taker_ratio(P, 14)

    # ── A. Delta coins, realistic charges ──
    liq = pd.read_csv(ROOT / "crypto" / "universe" / "delta_perps_liquidity.csv")
    liq = liq[liq["binance"].notna()].drop_duplicates("binance")
    allowed = set(liq.loc[liq["turnover_24h_usd"] >= 50e3, "binance"])
    trd = tr.copy()
    trd.loc[:, [c for c in trd.columns if c not in allowed]] = np.nan
    li = liq.set_index("binance")
    spread, depth = li["spread_bps"].reindex(P.close.columns), li["top_depth_usd"].reindex(P.close.columns)
    binance_fund = P.funding
    dfund, _ = delta_funding(P, liq)
    res = {}
    for v2 in (False, True):
        w = weights(P, trd, v2, B)
        dw = w.loc["2024-01-01":].diff().abs()
        order_usd = (dw.where(dw > 1e-9).mean() * CAP).fillna(0)
        cost = (TAKER_BPS + spread / 2 + (spread / 2) * (order_usd / depth).clip(upper=4)).fillna(30.0)
        P.funding = dfund
        out = E.run(P, w, cost).resample("1D").sum().loc["2024-01-01":]
        res[("A", v2)] = summary(out["net"])
        res[("A", v2)]["turnover"] = out["turnover"].sum() / (len(out) / 365)
    # ── B. full PIT history, flat 15 bps ──
    P.funding = binance_fund
    for v2 in (False, True):
        r = E.run(P, weights(P, tr, v2, B), 15.0)["net"].resample("1D").sum().loc["2020-03-01":]
        res[("B", v2)] = summary(r)
    btc = {k: CAP * (P.open["BTCUSDT"].pct_change().shift(-1).fillna(0).resample("1D").sum()
                     .loc[s:] + 1).cumprod() for k, s in (("A", "2024-01-01"), ("B", "2020-03-01"))}

    for sec, title in (("A", "Delta coins, Jan 2024 -> now, Delta realistic charges"),
                       ("B", "Point-in-time top-100, Mar 2020 -> now, 15 bps")):
        v1, v2 = res[(sec, False)], res[(sec, True)]
        print(f"\n=== {title} — $10,000 start ===")
        print(f"{'':<16}{'v1 (now)':>14}{'v2':>14}")
        for k, f in (("end", "${:,.0f}"), ("CAGR", "{:+.1%}"), ("maxDD", "{:.1%}"), ("Sharpe", "{:.2f}"),
                     ("worst_month", "{:+.1%}"), ("best_month", "{:+.1%}")):
            print(f"{k:<16}{f.format(v1[k]):>14}{f.format(v2[k]):>14}")
        print(f"{'months up':<16}{f'{int(round(v1['months_up'] * v1['n_months']))}/{v1['n_months']}':>14}"
              f"{f'{int(round(v2['months_up'] * v2['n_months']))}/{v2['n_months']}':>14}")
        for y in v1["years"]:
            print(f"{'  ' + str(y):<16}{v1['years'][y]:>+14.1%}{v2['years'][y]:>+14.1%}")

    fig, axes = plt.subplots(2, 2, figsize=(15, 8.5), height_ratios=[3, 1.3], facecolor=SURFACE)
    for col, (sec, title) in enumerate((("A", "Delta coins · 2024 → now · Delta realistic charges"),
                                         ("B", "All PIT top-100 · 2020 → now · 15 bps"))):
        a1, a2 = axes[0, col], axes[1, col]
        for ax in (a1, a2):
            _style(ax)
            ax.xaxis.set_major_locator(mdates.YearLocator())
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        for v2, c, name in ((False, C1, "v1 — running now"), (True, C2, "v2 — staggered + beta-neutral")):
            eq = res[(sec, v2)]["eq"]
            a1.plot(eq.index, eq.values, color=c, lw=2, label=f"{name}  ${eq.iloc[-1]:,.0f}")
            dd = eq / eq.cummax() - 1
            a2.plot(dd.index, dd.values, color=c, lw=1.4)
        b = btc[sec]
        a1.plot(b.index, b.values, color=CG, lw=1.2, label=f"BTC buy & hold  ${b.iloc[-1]:,.0f}")
        a1.set_yscale("log")
        a1.yaxis.set_major_formatter(_usd)
        a1.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        a1.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1, 2, 5)))
        a1.set_title(title, loc="left", fontsize=11.5, color=INK, fontweight="bold")
        a1.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK2)
        a2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
        a2.set_title("Drawdown", loc="left", fontsize=10, color=INK)
    fig.suptitle("Taker Flow L/S — v1 vs v2, $10,000 start ($5k long + $5k short)",
                 x=0.01, ha="left", fontsize=12.5, color=INK)
    fig.tight_layout()
    out = OUT / "compare_v1_v2.png"
    fig.savefig(out, dpi=130, facecolor=SURFACE)
    print("\n" + str(out))


if __name__ == "__main__":
    main()
