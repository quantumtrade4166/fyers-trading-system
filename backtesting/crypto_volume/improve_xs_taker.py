import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Can xs_taker (Taker Flow L/S) get a smaller drawdown and more positive months?

Each idea is tested ALONE on top of the live base, then in combination:
  base        14d taker-buy ratio, top/bottom 20%, inverse-vol, rebalance every 3 days
  stagger     3 sub-books rebalancing on days 0/1/2 of the cycle, averaged (one netted book)
  cap         no coin above `cap` of its book (excess spread over the rest of the book)
  beta        long and short books scaled so their market betas match (gross unchanged)
  blend       score = average cross-sectional rank of the 7/14/30-day ratios
  voltarget   whole book scaled by target / trailing 30d strategy vol (set at rebalances)

Fair comparison: every curve is ALSO shown scaled to the base's volatility, so a
lower drawdown can't come from simply holding less. % months up is scale-free.
Universe: point-in-time top-100 Binance perps, 15 bps/side + Binance funding.
IS = 2020-03..2023-12, OOS = 2024-01..

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.improve_xs_taker
"""

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from . import engine as E
from .robust_xs_taker import taker_ratio

OUT = Path(__file__).parent / "results"
L = 0.5
COST = 15.0
REBAL = 3
START = "2020-03-01"


# ── weight transforms ────────────────────────────────────────────────────
def stagger(P, score, n=REBAL):
    return sum(E.xs_weights(P, score, REBAL, offset=k) for k in range(n)) / n


def cap_weights(w: pd.DataFrame, cap: float) -> pd.DataFrame:
    """Per row and per book: no |w| above cap x book total; excess redistributed."""
    out = w.copy()
    arr = np.array(out.to_numpy(), dtype=float, copy=True)
    for r in range(arr.shape[0]):
        row = arr[r]
        for sign in (1, -1):
            idx = np.where(row * sign > 0)[0]
            if len(idx) < 2:
                continue
            x = np.abs(row[idx])
            tot = x.sum()
            lim = cap * tot
            if lim * len(idx) < tot:                 # cap infeasible with this few coins
                lim = tot / len(idx)
            for _ in range(20):
                over = x > lim + 1e-12
                if not over.any():
                    break
                excess = (x[over] - lim).sum()
                x[over] = lim
                free = ~over & (x < lim)
                if not free.any():
                    break
                x[free] += excess * x[free] / x[free].sum()
            row[idx] = sign * x
    return pd.DataFrame(arr, index=w.index, columns=w.columns)


def betas(P, days=60):
    mkt = P.ret.where(P.active).mean(axis=1)
    r = P.ret
    cov = r.rolling(days, min_periods=days // 2).cov(mkt)
    var = mkt.rolling(days, min_periods=days // 2).var()
    return cov.div(var, axis=0).shift(1)             # known before the bar


def beta_neutral(w: pd.DataFrame, beta: pd.DataFrame) -> pd.DataFrame:
    """Scale books so beta_long = beta_short; gross exposure unchanged. Factor set
    only where the weights change (rebalance rows) and held, so no daily churn."""
    lw, sw = w.clip(lower=0), -w.clip(upper=0)
    bl = (lw * beta).sum(axis=1)
    bs = (sw * beta).sum(axis=1)
    gl, gs = lw.sum(axis=1), sw.sum(axis=1)
    ok = (bl > 0) & (bs > 0) & (gl > 0) & (gs > 0)
    # k_l * bl = k_s * bs  and  k_l*gl + k_s*gs = gl + gs
    k_l = ((gl + gs) / (gl + gs * bl / bs)).where(ok)
    k_s = (k_l * bl / bs).where(ok)
    changed = w.ne(w.shift()).any(axis=1)
    k_l = k_l.where(changed).ffill().fillna(1.0).clip(0.4, 1.6)
    k_s = k_s.where(changed).ffill().fillna(1.0).clip(0.4, 1.6)
    return lw.mul(k_l, axis=0) - sw.mul(k_s, axis=0)


def vol_target(P, w: pd.DataFrame, days=30) -> pd.DataFrame:
    base = E.run(P, w, COST)["net"]
    rv = base.rolling(days, min_periods=10).std().shift(1)
    target = rv.loc[START:].median()
    s = (target / rv).clip(0.5, 1.5)
    changed = w.ne(w.shift()).any(axis=1)
    s = s.where(changed).ffill().fillna(1.0)
    return w.mul(s, axis=0)


# ── metrics ──────────────────────────────────────────────────────────────
def stats(r: pd.Series, match_vol: float | None = None) -> dict:
    r = r.loc[START:]
    if match_vol:
        r = r * match_vol / r.std()
    eq = (1 + r).cumprod()
    yrs = len(r) / 365
    m = (1 + r).resample("ME").prod() - 1
    sh = lambda x: x.mean() / x.std() * np.sqrt(365) if x.std() > 0 else np.nan
    return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "vol": r.std() * np.sqrt(365),
            "Sharpe": sh(r), "IS": sh(r.loc[:"2023-12-31"]), "OOS": sh(r.loc["2024-01-01":]),
            "maxDD": (eq / eq.cummax() - 1).min(), "months_up": (m > 0).mean(),
            "worst_month": m.min(), "months_up_OOS": (m.loc["2024":] > 0).mean(),
            "worst_month_OOS": m.loc["2024":].min()}


def main() -> None:
    P = E.Panel("1d", universe="pit")
    tr = {lb: taker_ratio(P, lb) for lb in (7, 14, 30)}
    blend_score = sum(t.where(P.active).rank(axis=1, pct=True) for t in tr.values()) / 3
    B = betas(P)

    def build(stag=False, cap=None, beta=False, blend=False, vt=False):
        score = blend_score if blend else tr[14]
        w = stagger(P, score) if stag else E.xs_weights(P, score, REBAL)
        if cap:
            w = cap_weights(w, cap)
        if beta:
            w = beta_neutral(w, B)
        w = L * w
        if vt:
            w = vol_target(P, w)
        return w

    rows, daily = {}, {}
    flags = ["stag", "cap", "beta", "blend", "vt"]
    configs = {"base": {}}
    for f in flags:
        configs[f] = {f: (0.2 if f == "cap" else True)}
    configs["cap 0.15"] = {"cap": 0.15}
    configs["cap 0.30"] = {"cap": 0.30}
    for a, b in itertools.combinations(flags, 2):
        configs[f"{a}+{b}"] = {a: (0.2 if a == "cap" else True), b: (0.2 if b == "cap" else True)}
    configs["stag+cap+beta"] = {"stag": True, "cap": 0.2, "beta": True}
    configs["stag+cap+beta+blend"] = {"stag": True, "cap": 0.2, "beta": True, "blend": True}
    configs["all five"] = {"stag": True, "cap": 0.2, "beta": True, "blend": True, "vt": True}

    base_vol = None
    for name, kw in configs.items():
        r = E.run(P, build(**kw), COST)["net"].resample("1D").sum()
        daily[name] = r
        if base_vol is None:
            base_vol = r.loc[START:].std()
        raw, mv = stats(r), stats(r, base_vol)
        rows[name] = {"Sharpe": raw["Sharpe"], "IS": raw["IS"], "OOS": raw["OOS"],
                      "CAGR": raw["CAGR"], "maxDD": raw["maxDD"],
                      "CAGR@baseRisk": mv["CAGR"], "maxDD@baseRisk": mv["maxDD"],
                      "months_up": raw["months_up"], "worst_month@baseRisk": mv["worst_month"],
                      "months_up_OOS": raw["months_up_OOS"]}
        x = rows[name]
        print(f"{name:<22} Sh {x['Sharpe']:.2f} IS {x['IS']:.2f} OOS {x['OOS']:.2f} | "
              f"@base risk: CAGR {x['CAGR@baseRisk']:6.1%} DD {x['maxDD@baseRisk']:6.1%} "
              f"worst mo {x['worst_month@baseRisk']:6.1%} | months up {x['months_up']:.0%} "
              f"(OOS {x['months_up_OOS']:.0%})", flush=True)
    OUT.mkdir(exist_ok=True)
    pd.DataFrame(rows).T.to_csv(OUT / "improve_xs_taker.csv")
    pd.DataFrame(daily).to_parquet(OUT / "improve_xs_taker_daily.parquet")


if __name__ == "__main__":
    main()
