"""
core/shared.py — shared Fyers helpers, loaded by explicit path.
===============================================================

Same reasoning as `live/broker.py`: both strategy packages have a `core/`
subpackage, so a plain `import core.symbol_master` would resolve to whichever is
first on sys.path. Option symbols are the one thing that must never be wrong — a
mis-resolved symbol is a real order on the wrong contract — so these are loaded
from their exact file path.

    symbol_master   the ONLY source of option symbol strings (weekly and monthly
                    expiries use different formats; we never hand-build them)
    fyers_client    the read-only authenticated client (never triggers a login,
                    which would invalidate the VPS token and kill the live feed)
    dte_calculator  nearest expiry + days-to-expiry, already gated to DTE 0/1
"""

import sys
import importlib.util
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_SS = Path(__file__).resolve().parents[2] / "strangle_strategy"
_SS_CORE = _SS / "core"


def _load(name: str, deps: dict = None):
    """Load one module by path. `deps` pre-registers modules it imports by name
    (dte_calculator does `from core import symbol_master`), so the import resolves
    to the module we already loaded rather than to whatever `core` is on the path."""
    for mod_name, mod in (deps or {}).items():
        sys.modules[mod_name] = mod
    spec = importlib.util.spec_from_file_location(f"_dn_shared_{name}", _SS_CORE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


symbol_master = _load("symbol_master")
fyers_client = _load("fyers_client")
singleton = _load("singleton")

# Own single-instance port. The VPS venv double-launches every script under the
# system python, and two engines would double-subscribe the socket and race on the
# same order book — for live orders that means duplicate trades.
PORT_DN_ENGINE = 47653


def nearest_expiry_and_dte(index: str, today=None):
    """(expiry, dte) for the front contract. Reimplemented here in two lines
    rather than path-loading dte_calculator, whose `from core import symbol_master`
    would need the ambiguous `core` package registered globally — exactly the
    shadowing this module exists to avoid."""
    import datetime as _dt
    today = today or _dt.date.today()
    exp = symbol_master.nearest_expiry(index, today)
    return exp, (exp - today).days


def is_trade_day(index: str, today=None) -> tuple[bool, int, object]:
    """(tradeable, dte, expiry). This strategy trades DTE 0 and DTE 1 only."""
    exp, d = nearest_expiry_and_dte(index, today)
    return (d in (0, 1)), d, exp
