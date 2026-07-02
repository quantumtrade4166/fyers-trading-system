"""
reporting/sheets_logger.py
==========================

Maintain a Google Sheet of Vwap-Strangle results — one row per index per day.

Two sources of the same schema:
  - PAPER  : read the V2 tick-engine archive (`{date}_{index}_V2.json`) and log its
             simulated trades / P&L. Runs EOD after the final V2 archive is written.
  - LIVE   : (later, Zerodha phase) log the REAL punched orders — same row shape,
             Mode="LIVE" — so paper vs live sit side by side and slippage is visible.

The push is IDEMPOTENT: a row is keyed by (Date, Index, Mode); re-running a day
UPDATES that row instead of appending a duplicate. So the EOD job can run repeatedly.

Auth = OAuth (user consent), NOT a service account — this Google org blocks
service-account key creation (iam.disableServiceAccountKeyCreation).

Setup (one-time):
  1. Google Cloud Console -> enable "Google Sheets API"; create an OAuth *client*
     (type "Desktop app") and download it to  config/credentials.json.
  2. Run  python reporting/sheets_logger.py --auth  once locally. A browser opens;
     approve access. A refresh token is cached to  config/authorized_user.json.
  3. Put the sheet id in config/parameters.json -> "google_sheets": {"spreadsheet_id": "..."}.
  4. VPS (headless): copy BOTH credentials.json and authorized_user.json to the VPS
     config/ folder — the cached token refreshes silently, no browser needed there.

CLI:
  python reporting/sheets_logger.py --auth          # one-time browser auth (cache token)
  python reporting/sheets_logger.py                 # log today, both indices (PAPER)
  python reporting/sheets_logger.py --date 2026-07-02
  python reporting/sheets_logger.py --index NIFTY
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]           # .../strangle_strategy
REPO = Path(__file__).resolve().parents[3]           # G:\fyers_data_pipeline
sys.path.append(str(ROOT))

from data.chart_archive import ARCHIVE_DIR

_PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
_GS = _PARAMS.get("google_sheets", {})
_STRIKE_INTERVAL = _PARAMS.get("strike_interval", {})
_OAUTH_CLIENT_FILE = REPO / _GS.get("oauth_client_file", "config/credentials.json")
_OAUTH_TOKEN_FILE = REPO / _GS.get("oauth_token_file", "config/authorized_user.json")
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_CYCLES = 4                                        # 4 entry points per the strategy

# Flat column order. Per-cycle blocks are E{n}/X{n} time+price + points.
HEADER = (
    ["Date", "Index", "DTE", "Mode", "CE Strike", "PE Strike", "Combined Premium"]
    + [c for n in range(1, MAX_CYCLES + 1)
       for c in (f"E{n} Time", f"E{n} Price", f"X{n} Time", f"X{n} Price", f"P{n}")]
    + ["Open?", "Total Points", "EOD P&L (Rs)", "Lots", "Source", "Captured"]
)


# ── strike resolution ─────────────────────────────────────────────────────
def _strikes(rec: dict) -> tuple:
    """CE, PE strikes for the row. Preferred order:
      1. explicit ce_strike / pe_strike in the selection meta,
      2. computed from atm +/- otm_level * strike_interval (reliable),
      3. "" if neither is available.
    (Parsing the strike out of the Fyers symbol is unreliable — the date code and
    strike run together, e.g. NIFTY2670223550CE — so we don't.)"""
    sel = rec.get("selection", {}) or {}
    ce, pe = sel.get("ce_strike"), sel.get("pe_strike")
    if ce and pe:
        return ce, pe
    atm, n = sel.get("atm"), sel.get("otm_level")
    iv = _STRIKE_INTERVAL.get((rec.get("index") or "").upper())
    if atm and n and iv:
        return atm + n * iv, atm - n * iv
    return "", ""


# ── row building (pure — no network, unit-testable) ───────────────────────
def build_row(rec: dict, mode: str = "PAPER") -> list:
    """Turn one V2 archive record into a flat sheet row matching HEADER."""
    sel = rec.get("selection", {}) or {}
    pnl = rec.get("pnl", {}) or {}
    trades = pnl.get("trades", []) or []
    open_trade = pnl.get("open_trade")

    ce_strike, pe_strike = _strikes(rec)
    row = [
        rec.get("date", ""),
        rec.get("index", ""),
        sel.get("dte", ""),
        mode,
        ce_strike,
        pe_strike,
        sel.get("combined_premium", ""),
    ]

    # up to 4 cycles, in fill order; pad missing cycles with blanks
    by_fill = {t.get("fill_no"): t for t in trades}
    for n in range(1, MAX_CYCLES + 1):
        t = by_fill.get(n)
        if t:
            row += [t.get("entry_time", ""), t.get("entry_price", ""),
                    t.get("exit_time", ""), t.get("exit_price", ""), t.get("points", "")]
        elif open_trade and open_trade.get("fill_no") == n:
            # entered but still open at EOD (no exit)
            row += [open_trade.get("entry_time", ""), open_trade.get("entry_price", ""),
                    "OPEN", "", ""]
        else:
            row += ["", "", "", "", ""]

    row += [
        "YES" if open_trade else "",
        pnl.get("net_points", pnl.get("realized_points", "")),
        pnl.get("net_pnl", pnl.get("realized_pnl", "")),
        pnl.get("lots", ""),
        rec.get("version", "V2"),
        rec.get("captured_at", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    return row


def _load_v2(date_str: str, index: str) -> dict | None:
    p = ARCHIVE_DIR / f"{date_str}_{index.upper()}_V2.json"
    return json.loads(p.read_text()) if p.exists() else None


# ── Google Sheets push (one tab PER INDEX) ────────────────────────────────
def _worksheet_name(index: str) -> str:
    """Tab name for an index (default: the index name itself). PAPER and LIVE rows
    share the index tab, told apart by the Mode column."""
    return _GS.get("worksheet_names", {}).get(index.upper(), index.upper())


def _client():
    """gspread client via OAuth (user consent). The first call opens a browser to
    approve access; the refresh token is then cached in oauth_token_file so later
    runs (including headless on the VPS) reuse it silently. credentials.json is the
    OAuth *client* (Desktop app), not a service account."""
    import gspread
    if not _OAUTH_CLIENT_FILE.exists():
        raise FileNotFoundError(f"OAuth client file not found at {_OAUTH_CLIENT_FILE}")
    return gspread.oauth(
        credentials_filename=str(_OAUTH_CLIENT_FILE),
        authorized_user_filename=str(_OAUTH_TOKEN_FILE),
        scopes=_SCOPES,
    )


def authorize():
    """One-time interactive auth: opens the browser, caches the refresh token.
    Run once locally (python reporting/sheets_logger.py --auth), then copy
    authorized_user.json to the VPS alongside credentials.json."""
    _client()
    print(f"  [sheets] authorized — token cached at {_OAUTH_TOKEN_FILE}")


def _open_worksheet(index: str):
    """Open (creating if needed) the per-index worksheet, ensuring the header row."""
    import gspread

    sid = _GS.get("spreadsheet_id")
    if not sid:
        raise RuntimeError("config/parameters.json -> google_sheets.spreadsheet_id is not set")
    sh = _client().open_by_key(sid)
    ws_name = _worksheet_name(index)
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=len(HEADER))
    if ws.row_values(1) != HEADER:              # ensure header row
        ws.update("A1", [HEADER])
    return ws


def upsert_row(row: list, index: str, mode: str = "PAPER"):
    """Insert or replace the row keyed by (Date, Mode) inside the index tab —
    idempotent, so the EOD job can run repeatedly without duplicating."""
    ws = _open_worksheet(index)
    date_v, mode_v = str(row[0]), str(row[3])
    records = ws.get_all_values()
    target = None
    for i, r in enumerate(records[1:], start=2):        # skip header
        if len(r) >= 4 and r[0] == date_v and r[3] == mode_v:
            target = i
            break
    if target:
        ws.update(f"A{target}", [row])
        print(f"  [sheets] {index} updated row {target}: {date_v} {mode_v}")
    else:
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"  [sheets] {index} appended: {date_v} {mode_v}")


def log_paper_day(date_str: str = None, indices: list[str] = None):
    """Read the V2 archive(s) for the day and upsert one row per index tab."""
    date_str = date_str or dt.date.today().isoformat()
    indices = indices or ["NIFTY", "SENSEX"]
    logged = 0
    for idx in indices:
        rec = _load_v2(date_str, idx)
        if not rec:
            print(f"  [sheets] no V2 archive for {idx} {date_str} — skip")
            continue
        upsert_row(build_row(rec, mode="PAPER"), idx, mode="PAPER")
        logged += 1
    print(f"  [sheets] done — {logged} row(s) for {date_str}")
    return logged


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Log Vwap-Strangle V2 results to Google Sheets")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--index", default=None, help="NIFTY or SENSEX (default: both)")
    ap.add_argument("--auth", action="store_true", help="one-time browser auth (cache token)")
    a = ap.parse_args()
    if a.auth:
        authorize()
    else:
        log_paper_day(a.date, [a.index.upper()] if a.index else None)
