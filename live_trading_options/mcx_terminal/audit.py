"""
mcx_terminal/audit.py — the APPEND-ONLY audit log behind the screen's "Logs" button.
==================================================================================

One JSON line per event, one file per IST day: logs/mcx_audit/audit_YYYY-MM-DD.jsonl
  * every order / button op a screen sends (who, what, the result)
  * every live order sent to Kotak, every fill / reject / cancel / price change
  * every change in Kotak's positions as the terminal sees them, EXIT ALL steps, LIVE arm / disarm,
    risk-guard firing, engine start / daily reset
Files are only ever opened in append mode — nothing in the terminal rewrites or deletes them. They sit
under C:\\trading, which the VPS backup mirrors to Google Drive every 10 minutes (+ daily snapshots).
"""
import datetime as dt
import json
import threading
from pathlib import Path

from live_trading_options.mcx_terminal import config as C

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DIR = C.LOG_DIR / "mcx_audit"
_lock = threading.Lock()


def _path(day: str) -> Path:
    return DIR / f"audit_{day}.jsonl"


def write(kind: str, user: str = "", **fields):
    """Append one event. Never raises (the audit must never break trading)."""
    try:
        now = dt.datetime.now(IST)
        line = json.dumps({"t": now.strftime("%H:%M:%S.%f")[:-3], "kind": kind, "user": user, **fields},
                          default=str, ensure_ascii=False)
        with _lock:
            DIR.mkdir(parents=True, exist_ok=True)
            with open(_path(now.date().isoformat()), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


def days() -> list:
    if not DIR.exists():
        return []
    return sorted((p.stem[6:] for p in DIR.glob("audit_*.jsonl")), reverse=True)


def read(day: str, limit: int = 5000) -> list:
    """Newest first."""
    p = _path(day)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
    return out[-limit:][::-1]
