"""
tools/compare.py — the month's answer: A vs B vs C.
===================================================

Reads every completed cycle from data/results/cycles.jsonl and prints the
comparison the experiment exists to make.

WHY THE HEADLINE NUMBER IS NOT TOTAL P&L
The three profiles hold for very different lengths of time (7.7h vs 23.6h) and
collect different credit per cycle ($14 vs $30) because they sell contracts with
different amounts of life left. Ranking them on total dollars would mostly be
ranking them on how much risk they carried, which is not the question. So three
views are printed:

    total          what the book actually made — the number you would have banked
    per cycle      the average outcome of one decision to be short
    per hour       return per hour of exposure — the fair way to read a 7.7h
                   profile against a 23.6h one
    on risk        return against worst-case-both-stopped, which is the real
                   capital the strategy puts up per cycle

A profile can win one of these and lose another; that is information, not noise.

PARTIAL CYCLES ARE EXCLUDED FROM THE HEADLINE
A cycle flagged `late_start` began because the engine was started or restarted
mid-cycle, so a 23.6h profile may have held for 4 hours. Those are shown
separately and left out of the averages — including them would quietly turn B
into a second copy of A on exactly the days the engine was touched.

Run:  .venv/Scripts/python.exe live_trading_options/delta_btc/tools/compare.py
      ... --all         include late-start cycles in the headline
      ... --csv out.csv write the per-cycle table
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import math
import argparse
import statistics as st

RESULTS = ROOT / "data" / "results"
CYCLES = RESULTS / "cycles.jsonl"
ORDER = ["ist_day", "full_cycle", "continuous"]
LABEL = {"ist_day": "A  ist_day (09:30-17:10, 0DTE)",
         "full_cycle": "B  full_cycle (17:35-17:10)",
         "continuous": "C  continuous (17:35-17:10, re-enters)"}


def load() -> list:
    if not CYCLES.exists():
        return []
    out = []
    for line in CYCLES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def stats(rows: list, usd_inr: float) -> dict:
    """Everything derivable from one profile's completed cycles."""
    n = len(rows)
    if not n:
        return {"n": 0}
    pnl = [r["realized"] for r in rows]
    total = sum(pnl)
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    hours = sum(r.get("hours_held") or r.get("exposure_hours", 0) for r in rows)
    risk = (rows[0]["sl"] - rows[0]["target"]) * 0.001 * rows[0]["contracts"] * 2

    # equity path over cycles, for the drawdown
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)

    return {
        "n": n,
        "total": total,
        "total_inr": total * usd_inr,
        "mean": st.mean(pnl),
        "median": st.median(pnl),
        "stdev": st.pstdev(pnl) if n > 1 else 0.0,
        "win_rate": 100.0 * len(wins) / n,
        "avg_win": st.mean(wins) if wins else 0.0,
        "avg_loss": st.mean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses else math.inf,
        "best": max(pnl), "worst": min(pnl),
        "max_dd": dd,
        "hours": hours,
        "per_hour": total / hours if hours else 0.0,
        "risk_per_cycle": risk,
        "return_on_risk": 100.0 * st.mean(pnl) / risk if risk else 0.0,
        "fees": sum(r.get("fees", 0) for r in rows),
        "gross": sum(r.get("gross", r["realized"]) for r in rows),
        "legs": sum(r.get("legs", 0) for r in rows),
        "adjustments": sum(r.get("adjustments", 0) for r in rows),
        "stopped": sum(r.get("stopped_legs", 0) for r in rows),
        "ended_early": sum(1 for r in rows if r.get("ended_early")),
        # Sharpe over cycles, annualised by how many cycles fit in a year. Rough
        # by construction on a one-month sample — quoted as a shape, not a fact.
        "sharpe": ((st.mean(pnl) / st.pstdev(pnl)) * math.sqrt(365 * 24 / (hours / n))
                   if n > 1 and st.pstdev(pnl) > 0 and hours else 0.0),
    }


def bar(v: float, lo: float, hi: float, width: int = 22) -> str:
    """A zero-anchored bar, so a losing profile reads as losing at a glance."""
    if hi == lo:
        return " " * width
    zero = int(round((0 - lo) / (hi - lo) * width))
    pos = int(round((v - lo) / (hi - lo) * width))
    cells = [" "] * width
    lo_i, hi_i = sorted((zero, pos))
    for i in range(max(0, lo_i), min(width, max(hi_i, lo_i + 1))):
        cells[i] = "█" if v >= 0 else "░"
    if 0 <= zero < width:
        cells[zero] = "│" if cells[zero] == " " else cells[zero]
    return "".join(cells)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="include late-start (partial) cycles in the headline")
    ap.add_argument("--csv", help="write the per-cycle table to this path")
    args = ap.parse_args()

    params = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
    usd_inr = float(params.get("usd_inr", 88.5))

    rows = load()
    if not rows:
        print(f"\n  No completed cycles yet. Expected at {CYCLES}")
        print("  Start the engine:  .venv/Scripts/python.exe "
              "live_trading_options/delta_btc/engine.py\n")
        return

    partial = [r for r in rows if r.get("late_start")]
    usable = rows if args.all else [r for r in rows if not r.get("late_start")]

    days = sorted({(r.get("first_entry") or r["ended"])[:10] for r in rows})
    print(f"\n  BTC delta-neutral strangle — paper comparison")
    print(f"  Delta Exchange India · {len(rows)} cycles over {len(days)} days "
          f"({days[0]} to {days[-1]}) · USD, ₹{usd_inr:g}/$")
    if partial and not args.all:
        print(f"  {len(partial)} late-start cycle(s) excluded from the headline "
              f"(engine started mid-cycle) — pass --all to include")

    per = {}
    for name in ORDER:
        per[name] = stats([r for r in usable if r["profile"] == name], usd_inr)

    live = [n for n in ORDER if per[n]["n"]]
    if not live:
        print("\n  No complete cycles to compare yet.\n")
        return

    # ── headline ─────────────────────────────────────────────────────────
    print(f"\n  {'':<44}{'A':>12}{'B':>12}{'C':>12}")
    print("  " + "─" * 80)

    def row(label, key, fmt="{:>12.2f}", scale=1.0, suffix=""):
        cells = ""
        for n in ORDER:
            s = per[n]
            if not s["n"]:
                cells += f"{'—':>12}"
            else:
                v = s.get(key, 0) * scale
                cells += (fmt.format(v) if not math.isinf(v) else f"{'inf':>12}")
        print(f"  {label:<44}{cells}{suffix}")

    row("cycles completed", "n", "{:>12.0f}")
    row("total P&L  ($)", "total")
    row("total P&L  (₹)", "total_inr", "{:>12,.0f}")
    print("  " + "─" * 80)
    row("mean per cycle  ($)", "mean")
    row("median per cycle  ($)", "median")
    row("best cycle  ($)", "best")
    row("worst cycle  ($)", "worst")
    row("std dev per cycle  ($)", "stdev")
    print("  " + "─" * 80)
    row("win rate  (%)", "win_rate")
    row("avg win  ($)", "avg_win")
    row("avg loss  ($)", "avg_loss")
    row("profit factor", "profit_factor")
    print("  " + "─" * 80)
    row("exposure  (hours)", "hours", "{:>12.1f}")
    row("P&L per hour exposed  ($)", "per_hour", "{:>12.4f}")
    row("risk per cycle  ($)", "risk_per_cycle")
    row("return on risk per cycle  (%)", "return_on_risk")
    row("max drawdown  ($)", "max_dd")
    row("Sharpe (annualised, rough)", "sharpe")
    print("  " + "─" * 80)
    row("legs traded", "legs", "{:>12.0f}")
    row("adjustments (2x rule)", "adjustments", "{:>12.0f}")
    row("legs stopped out", "stopped", "{:>12.0f}")
    row("cycles ended early", "ended_early", "{:>12.0f}")
    row("gross P&L  ($)", "gross")
    row("fees paid  ($)", "fees")

    # ── the three rankings ───────────────────────────────────────────────
    print("\n  Ranking depends on the question you ask:")
    for label, key, unit in (("most money banked", "total", "$"),
                             ("best per cycle", "mean", "$"),
                             ("best per hour exposed", "per_hour", "$"),
                             ("best return on risk", "return_on_risk", "%")):
        rank = sorted(live, key=lambda n: -per[n][key])
        bits = "  >  ".join(f"{n} ({per[n][key]:+.4g}{unit})" for n in rank)
        print(f"    {label:<24} {bits}")

    # ── visual ───────────────────────────────────────────────────────────
    vals = [per[n]["total"] for n in live]
    lo, hi = min(vals + [0.0]), max(vals + [0.0])
    print("\n  Total P&L")
    for n in live:
        v = per[n]["total"]
        print(f"    {LABEL[n]:<42} {bar(v, lo, hi)} {v:+9.2f}  (₹{v * usd_inr:+,.0f})")

    # ── honesty about the sample ─────────────────────────────────────────
    smallest = min(per[n]["n"] for n in live)
    print()
    if smallest < 20:
        print(f"  ⚠ Smallest sample is {smallest} cycles. At this size the ranking "
              f"is noise as much as signal —")
        print(f"    a single stop-out moves a mean built on {smallest} numbers. "
              f"Let the month finish.")
    else:
        spread = max(per[n]["mean"] for n in live) - min(per[n]["mean"] for n in live)
        pooled = max((per[n]["stdev"] for n in live), default=0) or 1
        if spread < 0.5 * pooled:
            print(f"  ⚠ The gap between profiles ({spread:.2f}) is small next to "
                  f"the per-cycle noise ({pooled:.2f}).")
            print("    Treat this as 'no clear winner', not as a narrow win.")

    if partial:
        print(f"\n  Late-start cycles ({len(partial)}), excluded above:")
        for r in partial:
            print(f"    {r['profile']:<12} {r.get('first_entry', '?')}  "
                  f"held {r.get('hours_held', 0):.1f}h  ${r['realized']:+.2f}")

    if args.csv:
        import csv
        cols = ["profile", "cycle", "expiry", "first_entry", "ended", "hours_held",
                "realized", "gross", "fees", "legs", "adjustments", "stopped_legs",
                "fresh_entries", "ended_early", "kill_reason", "late_start"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in sorted(rows, key=lambda r: (r.get("first_entry") or "", r["profile"])):
                w.writerow(r)
        print(f"\n  per-cycle table -> {args.csv}")
    print()


if __name__ == "__main__":
    main()
