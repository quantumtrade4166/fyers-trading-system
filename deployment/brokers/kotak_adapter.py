"""
kotak_adapter.py
Reads Kotak Bhaiya + Kotak Rohit live positions from the tick-engine's own
snapshot files (no second Kotak login needed — the engine already holds the
real session and writes snapshots every ~0.4s).

The adapter reconstructs BrokerSnapshot from:
  {date}_{INDEX}_KOTAK.json        — Bhaiya's snapshot (cycles, MTM, orders)
  {date}_{INDEX}_KOTAK_ROHIT.json  — Rohit's snapshot

Margin info is taken from the TICK file (written alongside the LIVE file by
the engine every ~0.4s), which carries the latest limits() call result.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
from pathlib import Path
from datetime import date

from deployment.brokers.base import BrokerAdapter, BrokerSnapshot, Position


_LIVE_DIR = (Path(__file__).parent.parent.parent /
             "live_trading_options" / "strangle_strategy" / "data" / "live_state")


def _today() -> str:
    return date.today().isoformat()


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _snapshot_from_live(d: dict, broker_label: str, tag_prefix: str) -> BrokerSnapshot:
    """Build a BrokerSnapshot from a LIVE.json snapshot dict."""
    if not d:
        return BrokerSnapshot(broker_label, status="not_running",
                              message="No live snapshot for today")

    broker_ready = d.get("broker_ready", False)
    mode = d.get("mode", "paper")
    if not broker_ready and mode == "paper":
        return BrokerSnapshot(broker_label, status="not_configured",
                              message="Kotak engine not connected (paper mode)")

    # Build positions from cycles (each cycle = one SELL pair + one BUY pair)
    positions = []
    seen_orders = set()
    for cyc in (d.get("cycles") or []):
        if not cyc.get("live") and not cyc.get("reconciled"):
            continue  # paper cycle — don't show in terminal
        for leg_key, leg_side in [("ce", "SHORT"), ("pe", "SHORT")]:
            sym = cyc.get(f"entry_{leg_key}")
            if not sym:
                continue
            qty = int(d.get("qty", 0))
            if qty == 0:
                continue
            # Check if this cycle is still open (not exited)
            exited = cyc.get("exit_time") is not None
            if exited:
                continue  # closed cycle — skip from open positions
            avg_price = cyc.get(f"entry_{leg_key}", 0.0)
            # Current mark from the marks dict
            marks = d.get("marks") or {}
            ltp = marks.get(sym, 0.0)
            # P&L: (avg - ltp) * qty for short
            try:
                pnl = round((float(avg_price or 0) - float(ltp or 0)) * qty, 2)
            except (TypeError, ValueError):
                pnl = 0.0
            positions.append(Position(
                broker=broker_label,
                symbol=sym,
                exchange="NFO",
                product="NRML",
                side=leg_side,
                qty=qty,
                avg_price=round(float(avg_price or 0), 2),
                ltp=round(float(ltp or 0), 2),
                pnl=pnl,
                realised=0.0,
            ))

    # Also show the currently open leg if the snapshot tracks it
    open_pos = d.get("open")
    if open_pos:
        for leg_key in ("ce", "pe"):
            sym = open_pos.get(f"{leg_key}_symbol")
            if not sym:
                continue
            if any(p.symbol == sym for p in positions):
                continue  # already in cycle list
            qty = int(d.get("qty", 0))
            if qty == 0:
                continue
            marks = d.get("marks") or {}
            ltp = marks.get(sym, 0.0)
            entry_price = open_pos.get(f"entry_{leg_key}", 0.0)
            try:
                pnl = round((float(entry_price or 0) - float(ltp or 0)) * qty, 2)
            except (TypeError, ValueError):
                pnl = 0.0
            positions.append(Position(
                broker=broker_label,
                symbol=sym,
                exchange="NFO",
                product="NRML",
                side="SHORT",
                qty=qty,
                avg_price=round(float(entry_price or 0), 2),
                ltp=round(float(ltp or 0), 2),
                pnl=pnl,
                realised=0.0,
            ))

    # Margin: read from the tick file (written every 0.4s by the engine)
    tick_path = _LIVE_DIR / f"{d.get('date', _today())}_{d.get('index', 'NIFTY')}_{tag_prefix}_TICK.json"
    tick_data = _read_json(tick_path) or {}
    margin = tick_data.get("margin") or {}
    margin_used = float(margin.get("used", 0) or 0)
    margin_available = float(margin.get("available", 0) or 0)

    # If no margin in tick file, try to estimate from MTM
    if margin_used == 0 and margin_available == 0:
        mtm = float(d.get("mtm_pnl", 0) or 0)
        margin_used = max(0.0, -mtm)  # negative MTM = margin at risk

    status = "ok" if broker_ready else "paper"
    if mode == "paper":
        status = "paper"

    return BrokerSnapshot(
        broker=broker_label,
        status=status,
        message=f"mode={mode}",
        positions=positions,
        margin_used=round(margin_used, 2),
        margin_available=round(margin_available, 2),
    )


class KotakBhaiyaAdapter(BrokerAdapter):
    name = "kotak"

    def is_configured(self) -> bool:
        import os
        return bool(os.getenv("KOTAK_CONSUMER_KEY"))

    def fetch_snapshot(self) -> BrokerSnapshot:
        if not self.is_configured():
            return BrokerSnapshot(self.name, status="not_configured",
                                  message="KOTAK_CONSUMER_KEY missing in .env")
        d = _read_json(_LIVE_DIR / f"{_today()}_NIFTY_KOTAK.json")
        if not d:
            return BrokerSnapshot(self.name, status="not_running",
                                  message=f"No snapshot for today ({_today()})")
        return _snapshot_from_live(d, self.name, "KOTAK")


class KotakRohitAdapter(BrokerAdapter):
    name = "kotak_rohit"

    def is_configured(self) -> bool:
        import os
        return bool(os.getenv("KOTAK_CONSUMER_KEY_ROHIT"))

    def fetch_snapshot(self) -> BrokerSnapshot:
        if not self.is_configured():
            return BrokerSnapshot(self.name, status="not_configured",
                                  message="KOTAK_CONSUMER_KEY_ROHIT missing in .env")
        d = _read_json(_LIVE_DIR / f"{_today()}_NIFTY_KOTAK_ROHIT.json")
        if not d:
            return BrokerSnapshot(self.name, status="not_running",
                                  message=f"No snapshot for today ({_today()})")
        return _snapshot_from_live(d, self.name, "KOTAK_ROHIT")
