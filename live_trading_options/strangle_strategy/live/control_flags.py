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
import datetime as dt
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "live_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONTROL_FILE = STATE_DIR / "live_control.json"      # legacy global (fallback)

_DEFAULT = {"mode": "paper", "kill": False, "updated": None}


def _control_file(index: str = None) -> Path:
    """PER-INDEX arm switch so NIFTY and SENSEX are armed independently. `index=None`
    falls back to the legacy global file (backward compat)."""
    if index:
        return STATE_DIR / f"live_control_{index.upper()}.json"
    return CONTROL_FILE


def read_control(index: str = None) -> dict:
    f = _control_file(index)
    if f.exists():
        try:
            d = json.loads(f.read_text())
            return {**_DEFAULT, **d}
        except Exception:
            pass
    return dict(_DEFAULT)


def write_control(index: str = None, mode: str = None, kill: bool = None) -> dict:
    c = read_control(index)
    if mode is not None:
        c["mode"] = "live" if str(mode).lower() == "live" else "paper"
    if kill is not None:
        c["kill"] = bool(kill)
    c["updated"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _control_file(index).write_text(json.dumps(c, indent=2))
    return c
