"""
order_router.py
Paper mode: logs orders to paper_orders.log.
Live mode: places orders via Fyers API (stub — enable when ready).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
from datetime import datetime
from pathlib import Path

import deployment.positions as positions

MODE     = os.getenv("TRADING_MODE", "paper").lower()
LOG_FILE = Path(__file__).parent / "logs" / "paper_orders.log"
LOG_FILE.parent.mkdir(exist_ok=True)


def _log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def execute_signal(
    pair_name: str,
    signal: str,
    price_a: float,
    price_b: float,
    qty_a: int,
    qty_b: int,
    current_z: float,
    beta: float,
    exit_reason: str = "",
) -> dict:
    """
    signal: 'long_spread' | 'short_spread' | 'exit' | 'stop'
    Returns action taken.
    """
    pos = positions.get_position(pair_name)

    # ── check if we need to exit existing position ────────────────────────────
    if pos and signal in ("exit", "stop"):
        trade = positions.close_position(
            pair_name, price_a, price_b, current_z,
            exit_reason or signal
        )
        if trade:
            span = (price_a * qty_a + price_b * qty_b) * 0.15
            _log(
                f"EXIT  {pair_name:30s}  reason={exit_reason or signal:12s}"
                f"  net_pnl=₹{trade['net_pnl']:>10,.0f}"
                f"  hold={trade['hold_days']}d"
                f"  z={current_z:.3f}"
                f"  SPAN=₹{span:,.0f}  mode={MODE}"
            )
            if MODE == "live":
                _place_live_order(pair_name, "exit", pos["direction"],
                                  price_a, price_b, qty_a, qty_b)
        return {"action": "exit", "trade": trade}

    # ── open new position ─────────────────────────────────────────────────────
    if not pos and signal in ("long_spread", "short_spread"):
        positions.open_position(
            pair_name, signal, price_a, price_b, qty_a, qty_b, current_z, beta
        )
        span = (price_a * qty_a + price_b * qty_b) * 0.15
        direction = "BUY A / SELL B" if signal == "long_spread" else "SELL A / BUY B"
        _log(
            f"ENTRY {pair_name:30s}  dir={direction:15s}"
            f"  z={current_z:.3f}  beta={beta:.4f}"
            f"  SPAN=₹{span:,.0f}  mode={MODE}"
        )
        if MODE == "live":
            _place_live_order(pair_name, "entry", signal,
                              price_a, price_b, qty_a, qty_b)
        return {"action": "entry", "signal": signal}

    return {"action": "none"}


def _place_live_order(pair_name, action, direction,
                      price_a, price_b, qty_a, qty_b) -> None:
    """Live Fyers order placement — stub until live mode enabled."""
    _log(f"  [LIVE STUB] {pair_name} {action} {direction} — NOT CONNECTED YET")
