"""
live/control_flags.py — cross-process control channel for the live engine.
=========================================================================

The dashboard (one process) and the live controller inside the V2 engine (another
process) can't call each other, so they talk through one small file:
    data/live_state/live_control.json  ->  {"mode": "paper"|"live", "kill": bool}

The dashboard WRITES it (KILL button, Paper/Live toggle); the controller READS it
(throttled) and obeys: kill -> flatten + stop; mode -> simulate vs place real orders.

Deliberately dead simple and file-based so it can never wedge the trading loop.
"""

import json
import datetime as _dt
import datetime as dt
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONTROL_FILE = STATE_DIR / "live_control.json"      # legacy global (fallback)

_DEFAULT = {"mode": "paper", "kill": False, "qty": None, "mtm_stop": None, "updated": None}
# qty / mtm_stop are OPTIONAL per-index overrides set from the dashboard before arming.
# None = "use the config default" (the controller keeps its parameters.json-derived
# qty and MTM stop). A number overrides it. The controller applies them ONLY while flat
# so an open position is never resized mid-trade.


def _control_file(index: str = None, state_dir: Path = None) -> Path:
    """PER-INDEX arm switch so NIFTY and SENSEX are armed independently. `index=None`
    falls back to the legacy global file (backward compat).

    `state_dir` lets a SECOND strategy reuse this exact control channel with its own
    files (the delta-neutral strangle keeps its flags under its own data/live_state),
    so both strategies share one implementation and can never read each other's arm
    switch. Default = the VWAP strangle's directory, unchanged."""
    d = state_dir or STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    if index:
        return d / f"live_control_{index.upper()}.json"
    return d / CONTROL_FILE.name


def _is_stale_kill(d: dict) -> bool:
    """True when `kill` was pressed on an EARLIER day.

    KILL means "stop trading NOW" — it is an action taken during a session, not a
    standing setting. It used to persist in the file forever, so a kill pressed in
    the evening silently disabled the whole of the next day: the engine started,
    read kill=true, and went straight to done without ever attempting an entry.
    That is exactly what happened on 2026-09-01 (pressed 08-31 20:16; the 09:30
    NIFTY 0-DTE entry never fired and it was only noticed at 09:33).

    A kill from today still stands — only yesterday's is cleared."""
    if not d.get("kill"):
        return False
    stamp = str(d.get("updated") or "")[:10]
    if not stamp:
        return True                     # no date to trust -> do not let it linger
    return stamp != _dt.date.today().isoformat()


def read_control(index: str = None, state_dir: Path = None) -> dict:
    f = _control_file(index, state_dir)
    if f.exists():
        try:
            d = {**_DEFAULT, **json.loads(f.read_text())}
            if _is_stale_kill(d):
                # clear it in the FILE too, so the dashboard and every other reader
                # see the same thing and it cannot come back on the next read
                d["kill"] = False
                d["kill_cleared_from"] = d.get("updated")
                try:
                    f.write_text(json.dumps(d, indent=2))
                except Exception:
                    pass
                print(f"  [control] {index}: stale KILL from {d.get('kill_cleared_from')} "
                      f"cleared — it was pressed on an earlier day", flush=True)
            return d
        except Exception:
            pass
    return dict(_DEFAULT)


def write_control(index: str = None, mode: str = None, kill: bool = None,
                  qty: int = None, mtm_stop: float = None,
                  state_dir: Path = None) -> dict:
    c = read_control(index, state_dir)
    if mode is not None:
        c["mode"] = "live" if str(mode).lower() == "live" else "paper"
    if kill is not None:
        c["kill"] = bool(kill)
    if qty is not None:                     # set an explicit size override (0/None-safe)
        c["qty"] = int(qty) if qty else None
    if mtm_stop is not None:                # set an explicit MTM-stop override
        c["mtm_stop"] = float(mtm_stop) if mtm_stop else None
    c["updated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _control_file(index, state_dir).write_text(json.dumps(c, indent=2))
    return c
