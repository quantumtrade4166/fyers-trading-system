"""
broker_eod.py
Daily end-of-day P&L snapshot for XTS (Jainam).

XTS resets its P&L every trading day, so we capture the day's final number at
15:30 IST and append it to deployment/xts_eod_pnl.json for historical tracking.
Zerodha + combined are recorded alongside for future use. Idempotent per date.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import threading
from datetime import datetime
from pathlib import Path

import pytz

EOD_FILE = Path(__file__).parent / "xts_eod_pnl.json"
IST = pytz.timezone("Asia/Kolkata")
_lock = threading.Lock()


def _read() -> dict:
    if not EOD_FILE.exists():
        return {"history": []}
    try:
        return json.loads(EOD_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"history": []}


def get_history() -> dict:
    return _read()


def record_eod() -> dict | None:
    """Snapshot today's XTS P&L. Skips (returns None) if XTS isn't connected,
    so an API hiccup never writes a spurious zero. Overwrites same-day entry."""
    from deployment.brokers import aggregator
    d = aggregator.get_terminal(force=True)
    by = {b["broker"]: b for b in d.get("brokers", [])}
    xts = by.get("jainam")
    if not xts or xts.get("status") != "ok":
        print(f"  [broker_eod] XTS not OK ({xts and xts.get('status')}) — EOD not recorded.")
        return None

    today = datetime.now(IST).strftime("%Y-%m-%d")
    rec = {
        "date":         today,
        "xts_pnl":      xts.get("total_pnl", 0),
        "xts_realised": xts.get("realised_pnl", 0),
        "xts_open":     xts.get("open_count", 0),
        "zerodha_pnl":  by.get("zerodha", {}).get("total_pnl", 0),
        "combined_pnl": d.get("combined_pnl", 0),
        "ts":           datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        data = _read()
        data["history"] = [r for r in data.get("history", []) if r.get("date") != today]
        data["history"].append(rec)
        data["history"].sort(key=lambda r: r.get("date", ""))
        EOD_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  [broker_eod] Recorded XTS EOD P&L for {today}: ₹{rec['xts_pnl']}")
    return rec


if __name__ == "__main__":
    r = record_eod()
    print("Done." if r else "Not recorded.")
