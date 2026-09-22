"""
tools/resume_dn.py — deliberately resume an index that halted itself today.
==========================================================================

A restart no longer resumes a halted day. The engine now restores the day's
decisions from its own snapshot, so a margin halt, a max-loss hit or a double
stop-out stays in force across a restart — which is the point: a crash-restart
must never quietly start trading again on a day the strategy had ended.

Resuming is therefore an explicit act. This clears the halt in TODAY'S live
snapshot for one index; restart the engine afterwards and it trades from the next
window. The KILL button is not handled here — that lives in the control file and
is cleared from the dashboard (GO LIVE).

The engine must be STOPPED while the snapshot is edited, or it writes the halt
straight back — the tool refuses to run otherwise:

    powershell -File live_trading_options\\tools\\restart_dn.ps1 -Stop
    .venv\\Scripts\\python.exe live_trading_options/delta_neutral/tools/resume_dn.py --index NIFTY
    schtasks /Run /TN DeltaNeutralEngine
"""
import sys
import json
import argparse
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

STATE = Path(__file__).resolve().parents[1] / "data" / "live_state"
PORT_DN_ENGINE = 47653          # the engine's single-instance lock (core/shared.py)


def engine_running() -> bool:
    """The running engine rewrites its snapshot every few seconds, so a halt cleared
    underneath it would simply be written back. It holds a TCP lock for its whole
    life; if we can bind that port, no engine is running."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORT_DN_ENGINE))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", required=True, choices=["NIFTY", "SENSEX"])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    f = STATE / f"{dt.date.today().isoformat()}_{a.index}_DN.json"
    if not f.exists():
        print(f"  no live snapshot for {a.index} today — nothing to resume")
        return 1
    d = json.loads(f.read_text(encoding="utf-8"))
    print(f"  {a.index}: killed={d.get('killed')} done={d.get('done')} "
          f"reason={d.get('kill_reason')!r} margin_halt={bool(d.get('margin_halt'))}")
    if not (d.get("killed") or d.get("done") or d.get("margin_halt")):
        print("  not halted — nothing to do")
        return 0
    if (d.get("kill_reason") or "") == "kill switch":
        print("  halted by the KILL button — clear it from the dashboard (GO LIVE) instead")
        return 1
    if a.dry_run:
        print("  DRY RUN — would clear the halt")
        return 0
    if engine_running():
        print("  the engine is running and would write the halt straight back.")
        print(r"  stop it first:  powershell -File live_trading_options\tools\restart_dn.ps1 -Stop")
        return 1

    d.update(killed=False, done=False, kill_reason=None, margin_halt=None)
    d.setdefault("events", []).append({"t": dt.datetime.now().strftime("%H:%M:%S"),
                                       "type": "resumed_by_hand",
                                       "note": "halt cleared with tools/resume_dn.py"})
    f.write_text(json.dumps(d, indent=2), encoding="utf-8")
    print("  halt cleared — start the engine:  schtasks /Run /TN DeltaNeutralEngine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
