"""
tools/reprocess_archive.py
==========================

Recompute events / P&L / MTM for already-archived chart files from their STORED
candles — no Fyers fetch needed. Use after changing the signal logic when the
underlying option contracts have expired (so a fresh archive is impossible) or
when no token is available.

Only events/pnl are rewritten; candles, VWAP and selection are untouched.

Usage:
    python tools/reprocess_archive.py                 # all files
    python tools/reprocess_archive.py 2026-06-25_NIFTY.json
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))
from core.signal_engine import simulate_day, pair_trades, mtm_series

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIR = ROOT / "data" / "chart_history"
_PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
_LOT = _PARAMS["lot_sizes"]


def reprocess(path: Path):
    d = json.loads(path.read_text())
    df = pd.DataFrame(d["candles"])
    df["datetime"] = pd.to_datetime(d["date"] + " " + df["time"])
    df["is_red"]     = df["close"] < df["open"]
    df["below_vwap"] = df["close"] < df["vwap"]
    df["above_vwap"] = df["close"] > df["vwap"]

    events = simulate_day(df, entry_cutoff=_PARAMS.get("entry_cutoff", "14:30"),
                          square_off=_PARAMS.get("square_off", "15:15"))
    lot = _LOT.get(d["index"].upper(), 1)
    pnl = pair_trades(events, lot_size=lot, lots=1)
    pnl["mtm"] = mtm_series(df, events, lot_size=lot, lots=1)
    d["events"] = events
    d["pnl"] = pnl
    path.write_text(json.dumps(d, indent=2))
    print(f"  {path.name}: {len(events)} events, net {pnl['net_points']} pts / "
          f"Rs {pnl['net_pnl']} (lot {lot})")


def main():
    targets = sys.argv[1:]
    files = ([ARCHIVE_DIR / t for t in targets] if targets
             else sorted(ARCHIVE_DIR.glob("*.json")))
    for f in files:
        if f.exists():
            reprocess(f)
        else:
            print(f"  missing: {f.name}")


if __name__ == "__main__":
    main()
