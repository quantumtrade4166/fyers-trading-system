"""test_control_flags.py — the KILL switch must not outlive its session.

OFFLINE, uses a temp directory; touches no real control file.

KILL means "stop trading NOW" — an action taken during a session, not a standing
setting. It used to persist forever, so a kill pressed in the evening silently
disabled the entire next day: the engine started, read kill=true, and went to
`done` without ever attempting an entry.

That is 2026-09-01 — pressed 08-31 20:16, and the 09:30 NIFTY 0-DTE entry never
fired. Nobody noticed until the broker terminal was checked at 09:33.

    .venv\\Scripts\\python.exe live_trading_options/strangle_strategy/live/test_control_flags.py
"""
import sys
import json
import shutil
import tempfile
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from control_flags import read_control, write_control, _is_stale_kill

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"    ok   {name}")
    else:
        FAIL += 1
        print(f"    FAIL {name}: got {got!r}, want {want!r}")


TODAY = dt.date.today().isoformat()
YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()

print("\n  -- _is_stale_kill --")
check("kill from TODAY still stands",
      _is_stale_kill({"kill": True, "updated": f"{TODAY} 20:16:01"}), False)
check("kill from YESTERDAY is stale",
      _is_stale_kill({"kill": True, "updated": f"{YESTERDAY} 20:16:01"}), True)
check("kill from long ago is stale",
      _is_stale_kill({"kill": True, "updated": "2026-01-04 11:00:00"}), True)
check("no kill is never stale", _is_stale_kill({"kill": False, "updated": YESTERDAY}), False)
check("kill with NO timestamp is treated as stale (nothing to trust)",
      _is_stale_kill({"kill": True}), True)

print("\n  -- read_control clears a stale kill, in memory AND on disk --")
tmp = Path(tempfile.mkdtemp())
try:
    f = tmp / "live_control_NIFTY.json"

    # the exact 2026-09-01 situation
    f.write_text(json.dumps({"mode": "live", "kill": True, "qty": 520,
                             "mtm_stop": 14000.0,
                             "updated": f"{YESTERDAY} 20:16:01"}))
    c = read_control("NIFTY", state_dir=tmp)
    check("yesterday's kill is cleared on read", c["kill"], False)
    check("arming is untouched", c["mode"], "live")
    check("size is untouched", c["qty"], 520)
    check("max loss is untouched", c["mtm_stop"], 14000.0)
    check("it records what it cleared", c.get("kill_cleared_from"), f"{YESTERDAY} 20:16:01")

    on_disk = json.loads(f.read_text())
    check("the FILE was rewritten, not just the return value", on_disk["kill"], False)
    check("a second read stays cleared", read_control("NIFTY", state_dir=tmp)["kill"], False)

    # a kill pressed TODAY must survive — this is the whole point of the switch
    f.write_text(json.dumps({"mode": "live", "kill": True, "qty": 520,
                             "updated": f"{TODAY} 10:05:00"}))
    c = read_control("NIFTY", state_dir=tmp)
    check("today's kill SURVIVES", c["kill"], True)
    check("and stays on disk", json.loads(f.read_text())["kill"], True)

    # write_control still works normally through the same path
    write_control("SENSEX", mode="live", kill=False, qty=180, state_dir=tmp)
    c = read_control("SENSEX", state_dir=tmp)
    check("write_control round-trips", (c["mode"], c["kill"], c["qty"]), ("live", False, 180))

    # a missing file is still the safe default
    c = read_control("BANKNIFTY", state_dir=tmp)
    check("missing file -> paper, not killed", (c["mode"], c["kill"]), ("paper", False))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
