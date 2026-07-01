"""
live_capture.py
===============

Intraday combined-premium capture for the Vwap Strangle dashboard.

Runs every couple of minutes during market hours (driven by the dashboard's
APScheduler). It rebuilds today's chart archive so the web app shows a
near-live chart (1-min-history resolution; ~2-min lag).

Efficiency: the strangle strikes are selected ONCE per day (at the first run
after 9:20, from the 9:15 candle close) and cached to data/intraday_state/.
Every later run reuses the cached strikes and only re-fetches those two legs —
so we never re-run the expensive OTM scan intraday, and the strikes stay fixed
for the whole day (per spec).
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.append(str(ROOT))

from core.fyers_client import get_client, token_status
from core.dte_calculator import nearest_expiry_and_dte
from core.strike_selector import (select_strangle_historical,
                                  select_strangle_live_quotes, threshold_for)
from data.chart_archive import archive_day

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDICES = ["NIFTY", "SENSEX"]
STATE_DIR = ROOT / "data" / "intraday_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


def _state_path(date_str: str, index: str) -> Path:
    return STATE_DIR / f"{date_str}_{index.upper()}.json"


def select_and_cache(client, index: str, date_str: str) -> dict:
    """Select the day's strangle once (at 9:20) and cache it; reuse on later calls.

    Uses LIVE quotes (real-time price at 9:20 — no history lag) so selection happens
    AT 9:20. Falls back to the historical reconstruction if live quotes fail (or for
    EOD/backfill). Never selects before 9:20 (the 9:15 candle hasn't closed yet)."""
    sp = _state_path(date_str, index)
    if sp.exists():
        return json.loads(sp.read_text())

    is_today = date_str == dt.date.today().isoformat()
    now = dt.datetime.now(IST)
    if is_today and now.time() < dt.time(9, 20):
        raise RuntimeError("before 9:20 — strike selection not due yet")

    exp, d = nearest_expiry_and_dte(index, dt.date.fromisoformat(date_str))
    thr = threshold_for(index, d if d in (0, 1) else 1)

    pick = None
    if is_today:                       # live day → real-time quotes AT 9:20
        try:
            pick = select_strangle_live_quotes(client, index, exp, thr)
            pick["source"] = "live_quotes"
        except Exception as e:
            print(f"  [select] {index} live quotes failed ({e}); using history")
    if pick is None:                   # EOD/backfill, or live-quote failure
        pick = select_strangle_historical(client, index, exp, thr, date_str)
        pick["source"] = "history"
    pick["dte"] = d
    sp.write_text(json.dumps(pick))
    return pick


def capture_index(client, index: str, date_str: str):
    pick = select_and_cache(client, index, date_str)
    meta = {k: pick.get(k) for k in
            ("spot", "atm", "otm_level", "combined_premium", "threshold", "dte")}
    archive_day(client, index, pick["ce_symbol"], pick["pe_symbol"],
                date_str, otm_level=pick["otm_level"], meta=meta)


def capture_all(date_str: str = None):
    """Capture both indices for today. Safe to call repeatedly; never raises."""
    date_str = date_str or dt.date.today().isoformat()
    st = token_status()
    if not st["valid"]:
        print(f"  [strangle-intraday] token invalid ({st['date']}) — skipping")
        return
    client = get_client()
    for idx in INDICES:
        try:
            capture_index(client, idx, date_str)
        except Exception as e:
            # holiday / pre-9:20 / no data — quietly skip this index this cycle
            print(f"  [strangle-intraday] {idx} skip: {e}")


if __name__ == "__main__":
    capture_all()
