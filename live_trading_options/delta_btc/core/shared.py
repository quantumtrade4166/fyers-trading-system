"""
core/shared.py — the small pieces this package borrows, and its own lock ports.
==============================================================================

Loaded BY EXPLICIT PATH, not by `import core.singleton`: three strategy packages
under `live_trading_options/` each have a `core/` subpackage, so a plain import
resolves to whichever happens to be first on sys.path. That is the silent
mis-wiring class of bug that ends with one strategy reading another's state.

`singleton` imports nothing but the stdlib, so loading it in isolation is safe.
"""

import sys
import importlib.util
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_SS_CORE = Path(__file__).resolve().parents[2] / "strangle_strategy" / "core"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_dbtc_shared_{name}",
                                                  _SS_CORE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


singleton = _load("singleton")

# Own lock ports, distinct from the dashboard (47651), the VWAP engine (47652)
# and the delta-neutral NSE engine (47653). Two collectors would double every
# archive row; two engines would run two books under one name.
PORT_BTC_COLLECTOR = 47654
PORT_BTC_ENGINE = 47655

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
ARCHIVE = DATA / "chain_archive"
STATE_DIR = DATA / "live_state"
LOGS = ROOT / "logs"
for _d in (ARCHIVE, STATE_DIR, LOGS):
    _d.mkdir(parents=True, exist_ok=True)

# Everything in this package thinks in IST, because that is the clock the daily
# settlement (17:30 IST) and every session profile are defined against.
import datetime as dt
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def now_ist() -> dt.datetime:
    """Naive IST — naive so it compares cleanly with the `dt.time` literals the
    session profiles are written in."""
    return dt.datetime.now(IST).replace(tzinfo=None)
