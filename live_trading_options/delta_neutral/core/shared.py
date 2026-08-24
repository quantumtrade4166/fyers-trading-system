"""
core/shared.py — the small pieces this strategy borrows from elsewhere.
======================================================================

This strategy is deliberately **Kite-only**: Kite for orders AND for market data.

Why that matters:
  - the VWAP strangle owns the Fyers WebSocket. A second Fyers socket on the same
    token risks the broker dropping one of them, and the strategy that loses its
    feed goes blind — no stop monitoring, no square-off. Using Kite's ticker
    removes that contention entirely.
  - prices then come from the same venue the orders go to, so a stop trigger is
    compared against the book it will actually execute against.
  - it drops the Fyers symbol master (and pandas) from the runtime path, which is
    what crashed the first VPS launch.

So the only thing borrowed here is the single-instance guard. Everything else the
engine needs comes from Kite via `core/chain.py`.
"""

import sys
import importlib.util
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

_SS_CORE = Path(__file__).resolve().parents[2] / "strangle_strategy" / "core"


def _load(name: str):
    """Load one module by explicit path. Both strategy packages have a `core/`
    subpackage, so a plain import would resolve to whichever is first on sys.path
    — the kind of silent mis-wiring that ends with orders going somewhere
    unintended. `singleton` imports nothing but the stdlib, so loading it in
    isolation is safe."""
    spec = importlib.util.spec_from_file_location(f"_dn_shared_{name}", _SS_CORE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


singleton = _load("singleton")

# Own single-instance port. The VPS venv double-launches every script, and two
# engines would double-subscribe the ticker and race on the same order book —
# for live orders that means duplicate trades.
PORT_DN_ENGINE = 47653
