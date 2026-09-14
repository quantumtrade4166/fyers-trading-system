"""
tools/reset_profiles.py — wipe named profiles' history, leave the rest untouched.
================================================================================

Used when a profile's rules change enough that its old trades would only muddy the
new ones: their cycles, equity curve, live book and event lines are removed from
the dashboard's view so the curve starts again from zero.

ARCHIVED, NOT DELETED. Everything removed is first copied to
data/archive/reset_<timestamp>/ — the originals, whole. Paper history is still the
record of what the old rules did, and a reset you cannot undo is a reset you will
eventually regret.

Surgical. Files shared by all profiles (cycles.jsonl, the daily audit logs) are
rewritten line by line keeping every line that does not belong to a named profile,
so a profile not named — ist_day, here — comes out byte-identical. The script
verifies that and aborts before writing anything if it does not hold.

The engine must be STOPPED while this runs, or it will rewrite the files it is
clearing a moment later.

    .venv/Scripts/python.exe live_trading_options/delta_btc/tools/reset_profiles.py full_cycle continuous
"""

import sys
import json
import shutil
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RESULTS = DATA / "results"
STATE = DATA / "live_state"
LOGS = ROOT / "logs"
KNOWN = {"ist_day", "full_cycle", "continuous"}


def owner(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line).get("profile")
    except json.JSONDecodeError:
        return None


def main():
    names = set(sys.argv[1:])
    if not names or not names <= KNOWN:
        print(f"usage: reset_profiles.py <profile> [...]   known: {sorted(KNOWN)}")
        return 2
    keep = KNOWN - names
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    arch = DATA / "archive" / f"reset_{stamp}"
    arch.mkdir(parents=True, exist_ok=True)
    print(f"  resetting {sorted(names)}; leaving {sorted(keep)} untouched")
    print(f"  archive -> {arch}")

    # ── shared line-per-record files: plan first, verify, then write ─────
    shared = [RESULTS / "cycles.jsonl"] + sorted(LOGS.glob("*_btc_audit.log"))
    plans = []
    for f in shared:
        if not f.exists():
            continue
        lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [l for l in lines if owner(l) not in names]
        # the safety net: every line of an untouched profile must survive, in order
        before = [l for l in lines if owner(l) in keep]
        after = [l for l in kept if owner(l) in keep]
        if before != after:
            print(f"  !! ABORT: {f.name} would lose {sorted(keep)} lines. Nothing written.")
            return 1
        plans.append((f, lines, kept))

    for f, lines, kept in plans:
        rel = f.relative_to(ROOT)
        (arch / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, arch / rel)
        f.write_text("".join(kept), encoding="utf-8")
        print(f"  {str(rel):<42} {len(lines):>6} -> {len(kept):>6} lines")

    # ── per-profile files: move them into the archive ────────────────────
    for name in sorted(names):
        for f in (RESULTS / f"{name}_equity.jsonl", STATE / f"{name}_RESUME.json",
                  STATE / f"{name}_STATE.json"):
            if f.exists():
                rel = f.relative_to(ROOT)
                (arch / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(arch / rel))
                print(f"  {str(rel):<42} archived")

    print("  done — start the engine; the reset profiles begin flat with an empty curve.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
