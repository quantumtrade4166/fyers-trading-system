"""
positions.py
Manages open positions and trade log via positions.json and trades.json.
Thread-safe via a simple lock.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import threading
from datetime import date, datetime
from pathlib import Path

POSITIONS_FILE = Path(__file__).parent / "positions.json"
TRADES_FILE    = Path(__file__).parent / "trades.json"
EQUITY_FILE    = Path(__file__).parent / "equity.json"

_lock = threading.Lock()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ── positions ─────────────────────────────────────────────────────────────────

def get_positions() -> dict:
    with _lock:
        return _read_json(POSITIONS_FILE, {})


def get_position(pair_name: str) -> dict | None:
    return get_positions().get(pair_name)


def open_position(pair_name: str, direction: str,
                  price_a: float, price_b: float,
                  qty_a: int, qty_b: int,
                  entry_z: float, beta: float) -> None:
    with _lock:
        pos = _read_json(POSITIONS_FILE, {})
        from deployment.pair_config import PAIRS, NAME, SYM_A, SYM_B
        sym_a = next((p[SYM_A] for p in PAIRS if p[NAME] == pair_name), "")
        sym_b = next((p[SYM_B] for p in PAIRS if p[NAME] == pair_name), "")
        pos[pair_name] = {
            "direction":     direction,
            "entry_date":    date.today().isoformat(),
            "entry_price_a": round(price_a, 2),
            "entry_price_b": round(price_b, 2),
            "qty_a":         qty_a,
            "qty_b":         qty_b,
            "entry_z":       round(entry_z, 4),
            "entry_beta":    round(beta, 4),
            "sym_a":         sym_a,
            "sym_b":         sym_b,
        }
        _write_json(POSITIONS_FILE, pos)


def close_position(pair_name: str, exit_price_a: float, exit_price_b: float,
                   exit_z: float, exit_reason: str) -> dict | None:
    with _lock:
        pos = _read_json(POSITIONS_FILE, {})
        if pair_name not in pos:
            return None

        p         = pos.pop(pair_name)
        direction = p["direction"]
        qty_a     = p["qty_a"]
        qty_b     = p["qty_b"]
        sign      = 1 if direction == "long_spread" else -1

        gross = ((exit_price_a - p["entry_price_a"]) * qty_a
                 - (exit_price_b - p["entry_price_b"]) * qty_b) * sign
        cost  = (p["entry_price_a"] * qty_a + p["entry_price_b"] * qty_b
                 + exit_price_a * qty_a + exit_price_b * qty_b) * 0.0003
        net   = round(gross - cost, 2)

        entry_dt = datetime.strptime(p["entry_date"], "%Y-%m-%d").date()
        hold     = (date.today() - entry_dt).days

        trade = {
            **p,
            "pair":         pair_name,
            "exit_date":    date.today().isoformat(),
            "exit_price_a": round(exit_price_a, 2),
            "exit_price_b": round(exit_price_b, 2),
            "exit_z":       round(exit_z, 4),
            "exit_reason":  exit_reason,
            "hold_days":    hold,
            "gross_pnl":    round(gross, 2),
            "net_pnl":      net,
        }

        _write_json(POSITIONS_FILE, pos)

        trades = _read_json(TRADES_FILE, [])
        trades.append(trade)
        _write_json(TRADES_FILE, trades)

        _append_equity(net)
        return trade


# ── trade log ─────────────────────────────────────────────────────────────────

def get_trades(limit: int = 100) -> list:
    trades = _read_json(TRADES_FILE, [])
    return list(reversed(trades))[:limit]


# ── equity curve ──────────────────────────────────────────────────────────────

def _append_equity(pnl: float) -> None:
    eq = _read_json(EQUITY_FILE, {"cumul_pnl": 0.0, "history": []})
    eq["cumul_pnl"] = round(eq["cumul_pnl"] + pnl, 2)
    eq["history"].append({
        "date": date.today().isoformat(),
        "pnl":  pnl,
        "cumul": eq["cumul_pnl"],
    })
    _write_json(EQUITY_FILE, eq)


def get_equity() -> dict:
    return _read_json(EQUITY_FILE, {"cumul_pnl": 0.0, "history": []})


# ── annual PnL tracker ────────────────────────────────────────────────────────

def get_annual_pnl(pair_name: str) -> float:
    """Sum of net_pnl for this pair in the current calendar year."""
    this_year = str(date.today().year)
    trades = _read_json(TRADES_FILE, [])
    return sum(
        t["net_pnl"] for t in trades
        if t.get("pair") == pair_name
        and str(t.get("exit_date", "")).startswith(this_year)
    )


def get_portfolio_annual_pnl() -> float:
    this_year = str(date.today().year)
    trades = _read_json(TRADES_FILE, [])
    return sum(
        t["net_pnl"] for t in trades
        if str(t.get("exit_date", "")).startswith(this_year)
    )
