"""
live/broker.py — the shared broker layer, loaded by explicit path.
==================================================================

This strategy reuses three modules that already run live money in the VWAP
strangle rather than growing a second copy of them:

    kite_executor   contract resolution from Kite's own dump, marketable-LIMIT
                    orders, place-verified (survives a lost HTTP response without
                    double-placing), resting SL orders, order status
    audit           append-only, flushed-per-line audit trail
    control_flags   the dashboard <-> engine control channel (arm / kill / size)

They are loaded FROM THEIR FILE PATH, not by `import live.kite_executor`, because
both packages have a `live/` subpackage: a plain import resolves to whichever is
first on sys.path, which is exactly the kind of silent mis-wiring that ends with
orders going somewhere unintended. An explicit path can only ever load the one
file named here.

Each of the three imports nothing from its own package, so loading them in
isolation is safe and stays safe.
"""

import sys
import importlib.util
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_SS_LIVE = Path(__file__).resolve().parents[2] / "strangle_strategy" / "live"
DN_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "live_state"
DN_STATE_DIR.mkdir(parents=True, exist_ok=True)

# this strategy's own order tag — the own-book scope. Never share it with the
# VWAP strangle's 'vwstrangle' or the two books would reconcile into each other.
TAG = "dnstrangle"
AUDIT_SYSTEM = "dn"


def _load(name: str):
    path = _SS_LIVE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_dn_shared_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kite_executor = _load("kite_executor")
_audit = _load("audit")
_control = _load("control_flags")


# ── audit: same file format, this strategy's own log file ────────────────
def audit_log(index, event: str, **fields):
    """One flushed line into data/audit/{date}_dn_audit.log. Never raises."""
    _audit.log(index, event, system=AUDIT_SYSTEM, **fields)


def audit_file(date_str: str = None) -> Path:
    return _audit.file_for(date_str, system=AUDIT_SYSTEM)


# ── control flags: same channel, this strategy's own state directory ─────
def read_control(index: str = None) -> dict:
    return _control.read_control(index, state_dir=DN_STATE_DIR)


def write_control(index: str = None, **kw) -> dict:
    return _control.write_control(index, state_dir=DN_STATE_DIR, **kw)
