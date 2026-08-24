"""
live/audit.py — permanent, append-only audit trail for the live strangle.
=========================================================================

Write-once, flushed on EVERY line, one file per trading day:
    data/audit/{YYYY-MM-DD}_audit.log

The single source of truth for troubleshooting. Records only REAL/live activity +
system lifecycle (engine start, strikes, arm/kill, real orders/fills/failures,
square-off, reconciliation) — never paper trades. Unlike the LIVE.json snapshot
(overwritten each persist) and the block-buffered stdout log (lost on os._exit),
every line here is flushed to disk immediately, so nothing is ever lost.

log() must NEVER raise — logging can't be allowed to break trading.
"""

import datetime as dt
from pathlib import Path

AUDIT_DIR = Path(__file__).resolve().parents[1] / "data" / "audit"
try:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def file_for(date_str: str = None, system: str = None) -> Path:
    """Today's audit file. `system` gives a strategy its OWN log (e.g. system='dn'
    -> {date}_dn_audit.log) so two strategies running side by side never interleave
    into one file — the whole value of this log is being readable at 3am."""
    d = date_str or dt.date.today().isoformat()
    return AUDIT_DIR / (f"{d}_{system}_audit.log" if system else f"{d}_audit.log")


def log(index, event: str, system: str = None, **fields):
    """Append ONE flushed line to today's audit log. Never raises."""
    try:
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        detail = "  ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
        line = f"{ts}  {str(index):<6}  {event:<20}  {detail}\n"
        with open(file_for(system=system), "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except Exception:
        pass
