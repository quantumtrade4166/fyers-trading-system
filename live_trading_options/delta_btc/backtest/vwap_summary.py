"""
backtest/vwap_summary.py — month-by-month table from the VWAP backtest run.

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\backtest\\vwap_summary.py --year 2026
    ... --csv out.csv     also write the per-day rows

A "day" is a settlement day: ist_day traded D 09:30-17:10, full_expiry D-1 17:35
-> D 17:10. Both sold the contract settling on D, so they are directly comparable.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse

VERSIONS = ("ist_day", "full_expiry")
LABEL = {"ist_day": "IST day (09:30-17:10)", "full_expiry": "Full expiry (17:35-17:10)"}


def max_dd(curve):
    peak = dd = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = min(dd, v - peak)
    return round(dd, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="2026")
    ap.add_argument("--csv")
    ap.add_argument("--tag", default="", help="results-file suffix, e.g. _median")
    a = ap.parse_args()

    f = ROOT / "data" / "vwap_results" / "backtest" / f"vwap_backtest_{a.year}{a.tag}.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    days = sorted(data)
    print(f"  {len(days)} settlement days in {a.year} "
          f"({days[0]} -> {days[-1]})\n")

    rows = []
    for day in days:
        d = data[day]
        if d.get("skip"):
            rows.append({"day": day, "skip": d["skip"]})
            continue
        r = {"day": day}
        for v in VERSIONS:
            c = d.get(v) or {}
            if c.get("skip"):
                r[v] = None
                r[v + "_skip"] = c["skip"]
                continue
            t = c["trades"]
            r[v] = round(c["net"], 2)
            r[v + "_trades"] = len(t)
            r[v + "_wins"] = sum(1 for x in t if x["net_usd"] > 0)
            r[v + "_fees"] = round(c["fees"], 2)
            r[v + "_over"] = bool(c.get("over_threshold"))
        rows.append(r)

    for v in VERSIONS:
        print(f"\n═══ {LABEL[v]} — {a.year} by month ═══")
        print(f"{'month':<9}{'days':>6}{'trades':>8}{'win %':>7}{'net $':>11}"
              f"{'fees $':>9}{'avg/day':>9}{'best':>9}{'worst':>9}{'max DD':>10}")
        run_curve, tot = [], {"d": 0, "t": 0, "w": 0, "net": 0.0, "fees": 0.0}
        months = sorted({r["day"][:7] for r in rows if r.get(v) is not None})
        for m in months:
            mine = [r for r in rows if r["day"][:7] == m and r.get(v) is not None]
            if not mine:
                continue
            nets = [r[v] for r in mine]
            trades = sum(r[v + "_trades"] for r in mine)
            wins = sum(r[v + "_wins"] for r in mine)
            fees = sum(r[v + "_fees"] for r in mine)
            curve, c = [], 0.0
            for n in nets:
                c += n
                curve.append(c)
            print(f"{m:<9}{len(mine):>6}{trades:>8}"
                  f"{(100 * wins / trades if trades else 0):>6.0f}%"
                  f"{sum(nets):>11,.0f}{fees:>9,.0f}{sum(nets) / len(mine):>9,.1f}"
                  f"{max(nets):>9,.0f}{min(nets):>9,.0f}{max_dd(curve):>10,.0f}")
            tot["d"] += len(mine); tot["t"] += trades; tot["w"] += wins
            tot["net"] += sum(nets); tot["fees"] += fees
            for x in nets:                   # one running total across the whole year
                run_curve.append(x + (run_curve[-1] if run_curve else 0.0))
        allnets = [r[v] for r in rows if r.get(v) is not None]
        print(f"{'YEAR':<9}{tot['d']:>6}{tot['t']:>8}"
              f"{(100 * tot['w'] / tot['t'] if tot['t'] else 0):>6.0f}%"
              f"{tot['net']:>11,.0f}{tot['fees']:>9,.0f}"
              f"{tot['net'] / max(1, tot['d']):>9,.1f}"
              f"{max(allnets):>9,.0f}{min(allnets):>9,.0f}{max_dd(run_curve):>10,.0f}")
        over = sum(1 for r in rows if r.get(v + "_over"))
        skips = [r.get(v + "_skip") for r in rows if r.get(v + "_skip")]
        print(f"  days where no pair was <= 100 (farthest pair used): {over}")
        if skips:
            top = {}
            for s in skips:
                top[s] = top.get(s, 0) + 1
            print(f"  days not traded: {len(skips)} — " +
                  ", ".join(f"{k} x{n}" for k, n in sorted(top.items(), key=lambda x: -x[1])))

    if a.csv:
        import csv
        keys = ["day"] + [k for v in VERSIONS for k in
                          (v, v + "_trades", v + "_wins", v + "_fees", v + "_over")]
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\n  per-day rows -> {a.csv}")


if __name__ == "__main__":
    main()
