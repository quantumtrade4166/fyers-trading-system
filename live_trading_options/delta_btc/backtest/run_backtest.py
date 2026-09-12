"""
backtest/run_backtest.py — run the strategy over the archived chain, and sweep.
==============================================================================

Answers the only question that matters right now: which parameters would actually
have made money on the BTC market we recorded, rather than the one we imagined.

Four days of live paper said the strategy wins nearly every adjustment and loses
every stop, and that a stop costs 4-6x what an adjustment earns. That is a
parameter problem, and parameters are exactly what a replay can test in minutes
instead of a month.

Run:
    .venv/Scripts/python.exe live_trading_options/delta_btc/backtest/run_backtest.py
    ... --profile ist_day          just one session profile
    ... --sweep                    target x sl-multiple grid
    ... --days 2026-09-10,2026-09-11
    ... --verbose                  print every cycle

WHAT THE NUMBERS ARE AND ARE NOT
Fills cross the real recorded spread and pay the real fee schedule, so these are
net numbers, not gross. But the archive samples about once a minute while the live
engine reads a push feed, so a stop is noticed later here than it would be live
and fills wherever the next minute happens to be. Stop-outs are therefore
PESSIMISTIC. Read the ranking, not the absolute P&L.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import shutil
import tempfile
import argparse
import datetime as dt
import statistics as st

from core.sessions import SessionProfile
from live.controller import BTCController
from backtest.replay import ReplayChain, load_days, available_days, coverage

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
SETTLE = PARAMS.get("settlement_time_ist", "17:30")


def expiry_for(minute: dt.datetime) -> str:
    """The daily contract alive at this minute: today's until 17:30 IST, then
    tomorrow's — the same rule the live engine uses."""
    h, m = (int(x) for x in SETTLE.split(":"))
    d = minute.date()
    if (minute.hour, minute.minute) >= (h, m):
        d = d + dt.timedelta(days=1)
    return f"{d.day:02d}{d.month:02d}{d.year % 100:02d}"


def run_one(profile_name: str, cfg: dict, params: dict, df, verbose=False) -> dict:
    """Drive one profile through every archived minute. Returns its cycles."""
    prof = SessionProfile(profile_name, cfg)
    sandbox = Path(tempfile.mkdtemp(prefix="btc_bt_"))
    ctrl = BTCController(prof, params, out_dir=sandbox)

    chains: dict = {}
    minutes = sorted(df["minute"].unique())
    by_min = {m: g for m, g in df.groupby("minute")}

    for m in minutes:
        ts = m.to_pydatetime() if hasattr(m, "to_pydatetime") else m
        # a controller holding a position keeps the expiry it sold into
        code = ctrl.expiry if (ctrl.expiry and not ctrl.position.is_flat) else expiry_for(ts)
        g = by_min[m]
        rows = g[g["expiry"] == code]
        if rows.empty:
            continue
        ch = chains.get(code)
        if ch is None:
            ch = chains[code] = ReplayChain(code, span=int(params.get("chain_span", 20)),
                                            settle_ist=SETTLE)
        ch.load_snapshot(rows.to_dict("records"))
        if not ch.is_ready():
            continue
        try:
            ctrl.on_tick(ch, ts)
        except Exception as e:
            print(f"  !! {profile_name} step error at {ts}: {type(e).__name__}: {e}")

    # close out whatever the last minute left open, so the final cycle is recorded
    if minutes:
        last = minutes[-1]
        last = last.to_pydatetime() if hasattr(last, "to_pydatetime") else last
        ctrl._roll_cycle(None, last + dt.timedelta(minutes=1))

    cycles = ctrl.cycles_done
    shutil.rmtree(sandbox, ignore_errors=True)
    if verbose:
        for c in cycles:
            print(f"     {c.get('first_entry', '?')}  held {c.get('hours_held') or 0:>5.1f}h  "
                  f"legs {c['legs']:>2}  adj {c['adjustments']:>2}  stops {c['stopped_legs']:>2}  "
                  f"${c['realized']:>+9.2f}")
    return cycles


def summarise(cycles: list) -> dict:
    real = [c["realized"] for c in cycles]
    if not real:
        return {"cycles": 0, "total": 0.0}
    legs = sum(c["legs"] for c in cycles)
    stops = sum(c["stopped_legs"] for c in cycles)
    adj = sum(c["adjustments"] for c in cycles)
    return {
        "cycles": len(cycles), "total": round(sum(real), 2),
        "mean": round(st.mean(real), 2),
        "best": round(max(real), 2), "worst": round(min(real), 2),
        "wins": sum(1 for r in real if r > 0),
        "legs": legs, "adjustments": adj, "stops": stops,
        "fees": round(sum(c.get("fees", 0) for c in cycles), 2),
        "gross": round(sum(c.get("gross", 0) for c in cycles), 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", help="one profile only")
    ap.add_argument("--days", help="comma-separated YYYY-MM-DD")
    ap.add_argument("--sweep", action="store_true", help="target x sl-multiple grid")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--min-coverage", type=float, default=40.0,
                    help="skip days with less than this %% of minutes captured")
    args = ap.parse_args()

    days = args.days.split(",") if args.days else available_days()
    print(f"\n  loading archive: {', '.join(days)}")
    df = load_days(days)
    if df.empty:
        print("  no archived snapshots found — has the collector run?\n")
        return
    cov = coverage(df)
    print(f"  {len(df):,} contract-minutes\n")
    print(f"  {'day':<12}{'minutes':>9}{'% of day':>10}  window")
    keep = []
    for d, c in sorted(cov.items()):
        mark = "" if c["pct_of_day"] >= args.min_coverage else "   (thin — skipped)"
        if not mark:
            keep.append(d)
        print(f"  {d:<12}{c['minutes']:>9}{c['pct_of_day']:>9.1f}%  "
              f"{c['first']}-{c['last']}{mark}")
    if not keep:
        print("\n  every day is below the coverage floor — nothing to test.\n")
        return
    df = df[df["minute"].dt.date.astype(str).isin(keep)]
    print(f"\n  testing on {len(keep)} day(s): {', '.join(keep)}")

    profiles = [args.profile] if args.profile else list(PARAMS["sessions"])
    base = {k: v for k, v in PARAMS.items() if k != "sessions"}

    if not args.sweep:
        print("\n  " + "=" * 74)
        print(f"  CURRENT RULES  — target {PARAMS['sessions'][profiles[0]]['target_premium']}, "
              f"SL {PARAMS.get('sl_combined_multiple')}x combined")
        print("  " + "=" * 74)
        for name in profiles:
            cfg = dict(PARAMS["sessions"][name])
            cycles = run_one(name, cfg, base, df, verbose=args.verbose)
            s = summarise(cycles)
            if not s["cycles"]:
                print(f"  {name:<12} no completed cycles")
                continue
            print(f"  {name:<12} {s['cycles']:>2} cycles  ${s['total']:>+9.2f}  "
                  f"(mean ${s['mean']:>+7.2f}, won {s['wins']}/{s['cycles']})  "
                  f"legs {s['legs']:>3}  adj {s['adjustments']:>3}  stops {s['stops']:>2}  "
                  f"fees ${s['fees']:.2f}")
        print()
        return

    # ── the sweep ────────────────────────────────────────────────────────
    targets = [30, 40, 50, 70, 100]
    mults = [1.1, 1.2, 1.5, 2.0]
    for name in profiles:
        print("\n  " + "=" * 74)
        print(f"  SWEEP — {name}")
        print("  " + "=" * 74)
        print(f"  {'target':>7}" + "".join(f"{('x' + str(m)):>13}" for m in mults))
        best = None
        for tgt in targets:
            row = f"  {tgt:>7}"
            for mult in mults:
                cfg = dict(PARAMS["sessions"][name])
                cfg["target_premium"] = tgt
                cfg["sl_premium"] = tgt * 2
                prm = dict(base)
                prm["sl_combined_multiple"] = mult
                cycles = run_one(name, cfg, prm, df)
                s = summarise(cycles)
                row += f"{s['total']:>+10.2f}/{s['stops']:<2}"
                if s["cycles"] and (best is None or s["total"] > best[0]):
                    best = (s["total"], tgt, mult, s)
            print(row)
        if best:
            t, tgt, mult, s = best
            print(f"\n     best: target {tgt}, SL {mult}x combined  ->  ${t:+.2f}  "
                  f"({s['cycles']} cycles, {s['stops']} stops, {s['adjustments']} adjustments)")
        print("     cell = total P&L / number of stop-outs")
    print()


if __name__ == "__main__":
    main()
