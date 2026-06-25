"""
run_daily_archive.py
====================

Automated EOD chart-archive runner. For NIFTY and SENSEX it:
  1. finds the nearest weekly expiry + DTE
  2. selects the strangle strikes per the threshold rule (reconstructed from
     the 9:15 close — same logic the live strategy applies at 9:20)
  3. saves the combined-premium 5-min OHLCV + VWAP + signal events to the
     7-day rolling archive (data/chart_history/)

Schedule this once per trading day shortly after 15:30 IST. On an index's
own expiry day the contracts are removed by Fyers soon after close, so the
job should run promptly (≈15:31). Runs each index independently — one failing
never blocks the other.

Usage:
    python run_daily_archive.py                # today, both indices
    python run_daily_archive.py --date 2026-06-25
    python run_daily_archive.py --index SENSEX
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import datetime as dt
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))
from core.fyers_client import get_client, token_status
from core.dte_calculator import nearest_expiry_and_dte
from core.strike_selector import select_strangle_historical, threshold_for
from data.chart_archive import archive_day

INDICES = ["NIFTY", "SENSEX"]


def run_index(client, index: str, date_str: str, retention: int = 7):
    exp, d = nearest_expiry_and_dte(index, dt.date.fromisoformat(date_str))
    # threshold uses the DTE bucket (clamped to {0,1}); for DTE>=2 the strategy
    # is idle but we still archive a representative strangle for the chart.
    bucket = d if d in (0, 1) else 1
    thr = threshold_for(index, bucket)
    tag = "TRADE DAY" if d in (0, 1) else "no-trade (chart only)"
    print(f"[{index}] expiry {exp} | DTE {d} ({tag}) | threshold <= {thr}")

    pick = select_strangle_historical(client, index, exp, thr, date_str)
    print(f"  selected OTM{pick['otm_level']}: {pick['ce_symbol']} + {pick['pe_symbol']}"
          f"  (combined {pick['combined_premium']}, ATM {pick['atm']})")
    archive_day(client, index, pick["ce_symbol"], pick["pe_symbol"],
                date_str, otm_level=pick["otm_level"], retention_days=retention)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--index", choices=INDICES, default=None)
    ap.add_argument("--retention", type=int, default=7)
    args = ap.parse_args()

    st = token_status()
    print(f"Token: date={st['date']} valid={st['valid']} (exp {st['exp_ist']})")
    if not st["valid"]:
        print("!! Token expired — run fetch_fyers_token_VPS.bat. Aborting.")
        sys.exit(1)

    client = get_client()
    targets = [args.index] if args.index else INDICES
    failures = 0
    for idx in targets:
        try:
            run_index(client, idx, args.date, args.retention)
        except Exception as e:
            failures += 1
            print(f"[{idx}] FAILED: {e}")
    print(f"\nDone. {len(targets) - failures}/{len(targets)} archived for {args.date}.")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
