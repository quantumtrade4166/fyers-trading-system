# ============================================================
# backtesting/etf_strategies/red_week_dca.py
#
# "Red Week DCA" — the owner's own accumulation rule.
#
#   Every week Rs5,000 is committed. On a RED week close the whole
#   accrued pot is deployed into the ETF. On a GREEN week nothing is
#   bought and the Rs5,000 rolls forward, so the week after two green
#   weeks deploys 3 x Rs5,000.
#
# The weekly Rs5,000 leaves the pocket either way, so the external
# cashflow schedule is IDENTICAL to a plain weekly SIP. That makes XIRR
# a fair head-to-head: same money in, same dates, only the timing of
# entry differs.
#
#   python backtesting/etf_strategies/red_week_dca.py
#   python backtesting/etf_strategies/red_week_dca.py --symbol GOLDBEES
# ============================================================

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.append(str(Path(__file__).parent.parent.parent))

from etf.etf_loader import ETFLoader  # noqa: E402

RESULTS = Path(__file__).parent / "results"
CONTRIB = 5_000          # rupees committed per week
COST_BPS = 5             # buy-side costs: stamp + exchange + STT + GST (~0.05%)


# ── weekly bars ──────────────────────────────────────────────

def weekly_bars(daily: pd.DataFrame) -> pd.DataFrame:
    w = daily.resample("W-FRI").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"),
    ).dropna()
    return w


def red_masks(w: pd.DataFrame) -> dict[str, pd.Series]:
    return {
        "candle": w["close"] < w["open"],              # red weekly candle
        "wow": w["close"] < w["close"].shift(1),       # down vs last week's close
    }


# ── the engine ───────────────────────────────────────────────

def simulate(w: pd.DataFrame, buy_week: pd.Series, contrib: int = CONTRIB,
             cash_rate: float = 0.0, cost_bps: float = COST_BPS,
             period_days: float = 7.0) -> pd.DataFrame:
    """
    Walk the weeks. `buy_week` is True on weeks we deploy the pot
    (all-True reproduces a plain weekly SIP).

    Whole units only — NIFTYBEES cannot be bought fractionally — so the
    remainder stays in the pot and is carried forward.
    """
    period_cash = (1.0 + cash_rate) ** (period_days / 365.25) - 1.0
    pot = 0.0
    units = 0
    spent_total = 0.0
    rows = []

    for dt, row in w.iterrows():
        pot *= 1.0 + period_cash          # interest on cash waiting for a dip
        pot += contrib                     # this period's commitment

        bought, spent = 0, 0.0
        if bool(buy_week.loc[dt]):
            px = row["close"] * (1.0 + cost_bps / 10_000)
            bought = int(pot // px)
            if bought:
                spent = bought * px
                pot -= spent
                units += bought
                spent_total += spent

        rows.append({
            "date": dt, "close": row["close"], "buy": bool(buy_week.loc[dt]),
            "bought": bought, "spent": round(spent, 2), "pot": round(pot, 2),
            "units": units, "equity": round(units * row["close"], 2),
            "value": round(units * row["close"] + pot, 2),
            "invested": round(spent_total, 2),
        })

    return pd.DataFrame(rows).set_index("date")


# ── metrics ──────────────────────────────────────────────────

def xirr(dates, amounts, lo=-0.95, hi=3.0, tol=1e-7) -> float:
    """Money-weighted annual return — the right metric for irregular cashflows."""
    t0 = dates[0]
    days = [(d - t0).days for d in dates]

    def npv(r):
        return sum(a / (1.0 + r) ** (n / 365.25) for a, n in zip(amounts, days))

    if npv(lo) * npv(hi) > 0:
        return float("nan")
    for _ in range(300):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < tol:
            break
    return (lo + hi) / 2


def metrics(sim: pd.DataFrame, label: str, contrib: int = CONTRIB) -> dict:
    dates = list(sim.index) + [sim.index[-1]]
    flows = [-contrib] * len(sim) + [float(sim["value"].iloc[-1])]

    peak = sim["value"].cummax()
    dd = (sim["value"] / peak - 1.0).min()

    contributed = contrib * len(sim)
    final = float(sim["value"].iloc[-1])
    avg_cost = sim["invested"].iloc[-1] / sim["units"].iloc[-1] if sim["units"].iloc[-1] else float("nan")

    return {
        "variant": label,
        "weeks": len(sim),
        "buys": int(sim["buy"].sum()),
        "contributed": contributed,
        "deployed": round(float(sim["invested"].iloc[-1])),
        "idle_cash_end": round(float(sim["pot"].iloc[-1])),
        "units": int(sim["units"].iloc[-1]),
        "avg_cost": round(avg_cost, 2),
        "final_value": round(final),
        "gain_x": round(final / contributed, 3),
        "xirr": round(xirr(dates, flows) * 100, 2),
        "max_dd": round(dd * 100, 1),
    }


# ── main ─────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTYBEES")
    ap.add_argument("--start", default=None)
    ap.add_argument("--contrib", type=int, default=CONTRIB)
    args = ap.parse_args()

    daily = ETFLoader().load(args.symbol, start=args.start)
    w = weekly_bars(daily)
    masks = red_masks(w)

    print(f"{args.symbol}  |  {len(w)} weekly bars  |  "
          f"{w.index.min().date()} -> {w.index.max().date()}  |  "
          f"Rs{args.contrib:,}/week")
    print(f"red weeks: candle {int(masks['candle'].sum())} "
          f"({masks['candle'].mean():.0%})   "
          f"wow {int(masks['wow'].sum())} ({masks['wow'].mean():.0%})\n")

    runs, sims = [], {}
    for defn, mask in masks.items():
        for rate, rtag in [(0.0, "0%"), (0.06, "6%")]:
            label = f"RULE {defn:<6} cash {rtag}"
            sim = simulate(w, mask, args.contrib, rate)
            sims[label] = sim
            runs.append(metrics(sim, label, args.contrib))

    always = pd.Series(True, index=w.index)
    for rate, rtag in [(0.0, "0%"), (0.06, "6%")]:
        label = f"SIP  every  cash {rtag}"
        sim = simulate(w, always, args.contrib, rate)
        sims[label] = sim
        runs.append(metrics(sim, label, args.contrib))

    out = pd.DataFrame(runs)
    pd.set_option("display.width", 250)
    print(out.to_string(index=False))

    RESULTS.mkdir(parents=True, exist_ok=True)
    out.to_csv(RESULTS / f"red_week_dca_{args.symbol}_summary.csv", index=False)
    curves = pd.DataFrame({k: v["value"] for k, v in sims.items()})
    curves.to_csv(RESULTS / f"red_week_dca_{args.symbol}_curves.csv")

    best = out.loc[out["xirr"].idxmax()]
    sip = out[out["variant"].str.startswith("SIP")]["xirr"].max()
    print(f"\nbest: {best['variant']}  XIRR {best['xirr']}%   "
          f"vs best SIP {sip}%   edge {round(best['xirr'] - sip, 2)} pp")
    print(f"saved -> {RESULTS}")


if __name__ == "__main__":
    main()
