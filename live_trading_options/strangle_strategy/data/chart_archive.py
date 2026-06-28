"""
data/chart_archive.py
=====================

7-day rolling archive of the combined-premium chart for the strangle strategy.

For each trading day + index it stores the combined-premium 5-min OHLCV candles,
the VWAP per candle, and the entry/exit signal events for whichever strikes were
selected that day. The web app (Phase 2C) reads these JSON files to render the
historical chart. Files older than `retention_days` are pruned automatically.

NOTE ON OHLC FIDELITY: high/low here are reconstructed from 1-min history
(open & close are exact; high/low best-effort, see premium_builder). When the
live tick-based engine exists, this archive is the dataset we will use to verify
how much the high/low approximation actually moves signals / entry(`low-1`) / MTM.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

import pandas as pd

sys.path.append(str(Path(__file__).resolve().parents[1]))
from core.premium_builder import combined_for_strikes
from core.signal_engine import simulate_day, pair_trades, mtm_series

_PARAMS = json.loads((Path(__file__).resolve().parents[1] / "config" / "parameters.json").read_text())
_LOT_SIZES = _PARAMS["lot_sizes"]

ARCHIVE_DIR = Path(__file__).resolve().parent / "chart_history"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


def _archive_path(date_str: str, index: str) -> Path:
    return ARCHIVE_DIR / f"{date_str}_{index.upper()}.json"


def build_day_record(client, index: str, ce_sym: str, pe_sym: str,
                     date_str: str, otm_level=None, meta: dict = None) -> dict:
    """Fetch + build the combined-premium chart record for one index/day."""
    combined = combined_for_strikes(client, ce_sym, pe_sym, date_str, date_str)
    day = combined[combined["date"] == pd.to_datetime(date_str).date()]
    if day.empty:
        raise RuntimeError(f"No combined data for {index} {date_str}")

    candles = [{
        "time":   r["datetime"].strftime("%H:%M"),
        "open":   round(float(r["open"]), 2),
        "high":   round(float(r["high"]), 2),
        "low":    round(float(r["low"]), 2),
        "close":  round(float(r["close"]), 2),
        "volume": int(r["volume"]),
        "vwap":   round(float(r["vwap"]), 2),
    } for _, r in day.iterrows()]

    # actual simulated trade sequence (alternating entry->exit, max 4 cycles,
    # no entry after 2:30, force square-off at 3:15)
    events = simulate_day(day, entry_cutoff=_PARAMS.get("entry_cutoff", "14:30"),
                          square_off=_PARAMS.get("square_off", "15:15"))
    lot_size = _LOT_SIZES.get(index.upper(), 1)
    pnl = pair_trades(events, lot_size=lot_size, lots=1)
    pnl["mtm"] = mtm_series(day, events, lot_size=lot_size, lots=1)

    rec = {
        "date":        date_str,
        "index":       index.upper(),
        "ce_symbol":   ce_sym,
        "pe_symbol":   pe_sym,
        "otm_level":   otm_level,
        "captured_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "n_candles":   len(candles),
        "candles":     candles,
        "events":      events,
        "pnl":         pnl,
    }
    if meta:                       # spot/atm/threshold/combined_premium for transparency
        rec["selection"] = meta
    return rec


def archive_day(client, index: str, ce_sym: str, pe_sym: str,
                date_str: str = None, otm_level=None, retention_days: int = 0,
                meta: dict = None) -> Path:
    """Build and persist one index/day record. retention_days<=0 keeps all days
    forever (no pruning) — the user wants the full trade/chart history retained."""
    date_str = date_str or dt.date.today().isoformat()
    record = build_day_record(client, index, ce_sym, pe_sym, date_str, otm_level, meta)
    path = _archive_path(date_str, index)
    path.write_text(json.dumps(record, indent=2))
    if retention_days and retention_days > 0:
        prune(retention_days)
    print(f"  archived {index} {date_str}: {record['n_candles']} candles, "
          f"{len(record['events'])} signal events -> {path.name}")
    return path


def prune(retention_days: int):
    """Delete archive files older than retention_days (by date in filename).
    Only called when retention_days > 0; default is keep-forever."""
    cutoff = dt.date.today() - dt.timedelta(days=retention_days - 1)
    for f in ARCHIVE_DIR.glob("*.json"):
        try:
            d = dt.date.fromisoformat(f.stem.split("_")[0])
        except ValueError:
            continue
        if d < cutoff:
            f.unlink()
            print(f"  pruned old archive: {f.name}")


def list_archive() -> list[dict]:
    """For the web app: available (date, index) pairs, newest first."""
    out = []
    for f in sorted(ARCHIVE_DIR.glob("*.json"), reverse=True):
        parts = f.stem.split("_")
        out.append({"date": parts[0], "index": parts[1], "file": f.name})
    return out


def load_day(date_str: str, index: str) -> dict | None:
    path = _archive_path(date_str, index)
    return json.loads(path.read_text()) if path.exists() else None
