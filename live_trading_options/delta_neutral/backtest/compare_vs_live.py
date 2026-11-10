"""
compare_vs_live.py — backtest vs the real live paper book, same days.
=====================================================================

Validates the historical backtest against the delta-neutral PAPER (shadow) book
that has actually been running live since 12-Oct. Both are 1-lot, same strategy
code; the only differences are the data feed (Breeze 1-min close vs live Kite
ticks) and granularity. For every day they overlap it prints backtest P&L next to
the real paper P&L, the gap, and whether they agree on direction.

Inputs:
  - results/dn_nifty_backtest_details.json   (produced by run_backtest.py)
  - a live-paper JSON {records:[{date,index,dte,pnl}]} — fetch from the dashboard:
        ssh vps "... curl http://127.0.0.1:8000/api/dn/pnl_history" > live_paper.json
    (pass its path as argv[1]; default: results/live_paper.json)

Run:
    .venv\\Scripts\\python.exe live_trading_options/delta_neutral/backtest/compare_vs_live.py [live_paper.json]
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "results"


def main():
    live_path = Path(sys.argv[1]) if len(sys.argv) > 1 else RES / "live_paper.json"
    details = json.loads((RES / "dn_nifty_backtest_details.json").read_text())
    live = json.loads(live_path.read_text())
    recs = live.get("records", live) if isinstance(live, dict) else live

    # backtest per (date) NIFTY realized  (details keyed "YYYY-MM-DD_dte")
    bt = {}
    for k, r in details.items():
        bt[(r["date"], int(r["dte"]))] = r.get("realized")

    live_n = [r for r in recs if r.get("index") == "NIFTY" and r.get("dte") in (0, 1)]
    live_n.sort(key=lambda r: r["date"])

    print(f"{'date':12}{'dte':>4}{'backtest':>12}{'live paper':>12}{'diff':>11}  dir")
    print("-" * 60)
    rows = []
    for r in live_n:
        key = (r["date"], int(r["dte"]))
        b = bt.get(key)
        lv = r.get("pnl")
        if b is None:
            print(f"{r['date']:12}{r['dte']:>4}{'(no bt)':>12}{lv:>12.2f}")
            continue
        diff = round(b - lv, 2)
        agree = "✓" if (b >= 0) == (lv >= 0) else "✗"
        rows.append((b, lv, diff, agree))
        print(f"{r['date']:12}{r['dte']:>4}{b:>12.2f}{lv:>12.2f}{diff:>+11.2f}   {agree}")

    if rows:
        import statistics as st
        bsum = sum(x[0] for x in rows)
        lsum = sum(x[1] for x in rows)
        mad = st.mean(abs(x[2]) for x in rows)
        agree = sum(1 for x in rows if x[3] == "✓")
        print("-" * 60)
        print(f"{'TOTAL':12}{'':>4}{bsum:>12.2f}{lsum:>12.2f}{bsum-lsum:>+11.2f}")
        print(f"\n  overlapping days      : {len(rows)}")
        print(f"  cumulative backtest   : ₹{bsum:,.2f}")
        print(f"  cumulative live paper : ₹{lsum:,.2f}")
        print(f"  mean abs daily gap    : ₹{mad:,.2f}")
        print(f"  direction agreement   : {agree}/{len(rows)}  ({round(100*agree/len(rows))}%)")
        if len(rows) >= 3:
            bs = [x[0] for x in rows]; ls = [x[1] for x in rows]
            try:
                print(f"  daily correlation     : {st.correlation(bs, ls):+.3f}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
