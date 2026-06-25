"""
core/dte_calculator.py
======================

Days-to-expiry logic and valid-trading-day gate.

The strategy only trades DTE 0 or DTE 1:
    Index   | 0 DTE     | 1 DTE
    NIFTY   | Tuesday   | Monday
    SENSEX  | Thursday  | Wednesday
DTE 2+  -> idle ("No trade day").
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

from core import symbol_master


def dte(expiry: dt.date, today: dt.date = None) -> int:
    """Calendar days to expiry (0 = expiry day)."""
    today = today or dt.date.today()
    return (expiry - today).days


def nearest_expiry_and_dte(index: str, today: dt.date = None) -> tuple[dt.date, int]:
    today = today or dt.date.today()
    exp = symbol_master.nearest_expiry(index, today)
    return exp, dte(exp, today)


def is_trade_day(index: str, today: dt.date = None) -> tuple[bool, int, dt.date]:
    """Return (tradeable, dte, expiry). Tradeable only when dte in {0, 1}."""
    today = today or dt.date.today()
    exp, d = nearest_expiry_and_dte(index, today)
    return (d in (0, 1)), d, exp
