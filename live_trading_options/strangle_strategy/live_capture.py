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
import logging
import datetime as dt
from pathlib import Path
from logging.handlers import RotatingFileHandler

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

# Live quotes reflect the CURRENT premium; they only equal the 9:20 selection
# premium inside a short window right after the open. Past this, a late selection
# (e.g. after a scheduler miss) must reconstruct the 9:20 price from history —
# otherwise it picks strikes off the decayed mid-day premium, and the morning
# candles show inflated values (the "first candle = 223" bug).
LIVE_QUOTE_WINDOW_END = dt.time(9, 22)

# Persistent, rotating capture log — so a silent failure is never invisible again.
_LOG_DIR = ROOT.parents[1] / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
log = logging.getLogger("strangle_capture")
if not log.handlers:
    log.setLevel(logging.INFO)
    _h = RotatingFileHandler(_LOG_DIR / "strangle_capture.log",
                             maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)


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

    # Live quotes ONLY inside the 9:20 window; otherwise reconstruct the 9:20
    # price from history so a late selection still picks the correct 9:20 strikes.
    in_live_window = is_today and now.time() <= LIVE_QUOTE_WINDOW_END

    pick = None
    if in_live_window:                 # at ~9:20 → real-time quotes = 9:20 premium
        try:
            pick = select_strangle_live_quotes(client, index, exp, thr)
            pick["source"] = "live_quotes"
        except Exception as e:
            log.warning(f"{index} {date_str} live quotes failed ({e}); using 9:20 history")
    if pick is None:                   # late / EOD / backfill → 9:20 reconstruction
        pick = select_strangle_historical(client, index, exp, thr, date_str)
        pick["source"] = "history_920" if is_today else "history"
    pick["dte"] = d
    sp.write_text(json.dumps(pick))
    log.info(f"{index} {date_str} strikes selected via {pick['source']}: "
             f"OTM{pick['otm_level']} {pick['ce_symbol']}/{pick['pe_symbol']} "
             f"combined={pick['combined_premium']} thr={thr} dte={d}")
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
        log.error(f"token invalid ({st['date']}) — capture skipped for {date_str}")
        print(f"  [strangle-intraday] token invalid ({st['date']}) — skipping")
        return
    client = get_client()
    ok = 0
    for idx in INDICES:
        try:
            capture_index(client, idx, date_str)
            ok += 1
        except Exception as e:
            # holiday / pre-9:20 / no data — skip this index this cycle (LOGGED now)
            log.warning(f"{idx} {date_str} capture skipped: {e}")
            print(f"  [strangle-intraday] {idx} skip: {e}")
    log.info(f"capture cycle {date_str}: {ok}/{len(INDICES)} indices archived")


if __name__ == "__main__":
    capture_all()
