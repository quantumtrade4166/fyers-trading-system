# ============================================================
# backtesting/etf_strategies/compare_sip_frequency.py
#
# Head-to-head: the owner's weekly red-week rule vs plain SIPs at
# weekly and monthly frequency.
#
# The amounts are NOT equal by default -- Rs5,000/week is Rs2.60L/yr
# while Rs20,000/month is Rs2.40L/yr -- so this reports BOTH:
#   xirr    : money-weighted return, valid across unequal cashflows
#   gain_x  : final value per rupee contributed
# and adds a capital-matched monthly SIP (Rs21,667) for a clean test
# of frequency alone.
#
#   python backtesting/etf_strategies/compare_sip_frequency.py
# ============================================================

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.append(str(Path(__file__).parent.parent.parent))
sys.path.append(str(Path(__file__).parent))

from etf.etf_loader import ETFLoader                      # noqa: E402
from red_week_dca import RESULTS, metrics, simulate       # noqa: E402


def bars(daily: pd.DataFrame, rule: str) -> pd.DataFrame:
    """
    Period bars labelled by the ACTUAL last trading day of the period,
    not the calendar period end, so every XIRR uses real cashflow dates.
    """
    b = daily.resample(rule).agg(open=("open", "first"), close=("close", "last"))
    last = daily.index.to_series().resample(rule).last()
    b = b.assign(d=last.reindex(b.index).values).dropna(subset=["d"]).set_index("d")
    b.index.name = "date"
    return b.dropna()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTYBEES")
    ap.add_argument("--start", default=None)
    args = ap.parse_args()

    daily = ETFLoader().load(args.symbol, start=args.start)
    wk = bars(daily, "W-FRI")
    mo = bars(daily, "ME")

    print(f"{args.symbol}  |  {wk.index.min().date()} -> {daily.index.max().date()}  "
          f"|  {len(wk)} weeks / {len(mo)} months\n")

    # Capital-matched monthly amount: same rupees per year as Rs5k/week.
    matched = round(5_000 * len(wk) / len(mo) / 100) * 100

    plans = [
        # label,                         bars, mask,        contrib, cash, period_days
        ("RULE red week  Rs5k/wk  cash 0%", wk, wk["close"] < wk["open"], 5_000, 0.00, 7.0),
        ("RULE red week  Rs5k/wk  cash 6%", wk, wk["close"] < wk["open"], 5_000, 0.06, 7.0),
        ("SIP  weekly    Rs5k/wk",          wk, None,                     5_000, 0.00, 7.0),
        ("SIP  monthly   Rs20k/mo",         mo, None,                    20_000, 0.00, 30.44),
        (f"SIP  monthly   Rs{matched / 1000:g}k/mo (matched)",
                                            mo, None,                   matched, 0.00, 30.44),
    ]

    rows = []
    for label, b, mask, contrib, rate, pdays in plans:
        buy = pd.Series(True, index=b.index) if mask is None else mask.reindex(b.index).fillna(False)
        sim = simulate(b, buy, contrib, rate, period_days=pdays)
        m = metrics(sim, label, contrib)
        m["per_yr"] = round(contrib * len(b) / ((b.index[-1] - b.index[0]).days / 365.25))
        rows.append(m)

    out = pd.DataFrame(rows)[[
        "variant", "per_yr", "contributed", "buys", "units", "avg_cost",
        "final_value", "gain_x", "xirr", "max_dd",
    ]]
    pd.set_option("display.width", 250)
    print(out.to_string(index=False))

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"sip_frequency_{args.symbol}.csv"
    out.to_csv(path, index=False)

    rule = out.loc[out["variant"].str.startswith("RULE"), "xirr"].max()
    m20 = out.loc[out["variant"].str.contains("Rs20k"), "xirr"].iloc[0]
    mm = out.loc[out["variant"].str.contains("matched"), "xirr"].iloc[0]
    print(f"\nrule (best)            : {rule}%")
    print(f"monthly Rs20k          : {m20}%   -> rule edge {round(rule - m20, 2)} pp")
    print(f"monthly matched        : {mm}%   -> rule edge {round(rule - mm, 2)} pp")
    print(f"\nsaved -> {path}")


if __name__ == "__main__":
    main()
