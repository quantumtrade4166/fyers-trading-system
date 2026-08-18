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

# ── LIVE tab (the day's REAL Zerodha result, from the LIVE.json snapshots) ────────
LIVE_STATE_DIR = ROOT / "data" / "live_state"
LIVE_WS_NAME = _GS.get("live_worksheet", "Vwap Live Zerodha")
# One row per index per day in ONE shared tab, keyed by (Date, Index). Per-cycle blocks
# carry the actual combined FILLS (not simulated), plus qty + realized/open P&L.
LIVE_HEADER = (
    ["Date", "Index", "DTE", "Mode", "CE Symbol", "PE Symbol", "Qty"]
    + [c for n in range(1, MAX_CYCLES + 1)
       for c in (f"E{n} Time", f"E{n} Fill", f"X{n} Time", f"X{n} Fill", f"P{n} Pts", f"P{n} Rs")]
    + ["Cycles", "Net Points", "Realized P&L (Rs)", "Open MTM (Rs)",
       "Open?", "Killed", "Kill Reason", "Updated", "Captured"]
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


def _open_named_ws(ws_name: str, header: list):
    """Open (creating if needed) a worksheet by exact name, ensuring the header row."""
    import gspread

    sid = _GS.get("spreadsheet_id")
    if not sid:
        raise RuntimeError("config/parameters.json -> google_sheets.spreadsheet_id is not set")
    sh = _client().open_by_key(sid)
    try:
        ws = sh.worksheet(ws_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ws_name, rows=1000, cols=len(header))
    if ws.row_values(1) != header:              # ensure header row
        ws.update(range_name="A1", values=[header])
    return ws


def _open_worksheet(index: str):
    """The per-index PAPER/LIVE tab (schema = HEADER)."""
    return _open_named_ws(_worksheet_name(index), HEADER)


def _upsert(ws, row: list, key_idx: tuple, label: str = ""):
    """Insert or replace `row`, keyed by the column indices in `key_idx` — idempotent,
    so the EOD job can re-run without duplicating. (Paper: (Date, Mode) inside an index
    tab. Live: (Date, Index) inside the shared live tab.)"""
    keys = [str(row[i]) for i in key_idx]
    records = ws.get_all_values()
    target = None
    for i, r in enumerate(records[1:], start=2):        # skip header
        if all(len(r) > k and r[k] == keys[j] for j, k in enumerate(key_idx)):
            target = i
            break
    tag = " ".join(keys)
    if target:
        ws.update(range_name=f"A{target}", values=[row])
        print(f"  [sheets] {label} updated row {target}: {tag}")
    else:
        ws.append_row(row, value_input_option="USER_ENTERED")
        print(f"  [sheets] {label} appended: {tag}")


def upsert_row(row: list, index: str, mode: str = "PAPER"):
    """Insert or replace the row keyed by (Date, Mode) inside the index tab."""
    _upsert(_open_worksheet(index), row, key_idx=(0, 3), label=index)


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
        dte = (rec.get("selection") or {}).get("dte")
        if dte not in (0, 1):                       # DTE >= 2 = chart-only, not a trade day
            print(f"  [sheets] {idx} {date_str} DTE={dte} — chart-only, not logged")
            continue
        upsert_row(build_row(rec, mode="PAPER"), idx, mode="PAPER")
        logged += 1
    print(f"  [sheets] done — {logged} row(s) for {date_str}")
    return logged


# ── LIVE export (real broker result from the LIVE.json snapshot) ─────────────────
def _load_live(date_str: str, index: str) -> dict | None:
    p = LIVE_STATE_DIR / f"{date_str}_{index.upper()}_LIVE.json"
    return json.loads(p.read_text()) if p.exists() else None


def build_live_row(snap: dict) -> list:
    """One LIVE.json snapshot -> a flat row matching LIVE_HEADER. Pure (no network)."""
    cycles = snap.get("cycles", []) or []
    is_open = bool(snap.get("open"))
    row = [
        snap.get("date", ""),
        snap.get("index", ""),
        snap.get("dte", ""),
        str(snap.get("mode", "")).upper(),
        snap.get("ce_symbol", ""),
        snap.get("pe_symbol", ""),
        snap.get("qty", ""),
    ]
    by_no = {c.get("cycle"): c for c in cycles}
    for n in range(1, MAX_CYCLES + 1):
        c = by_no.get(n)
        if c:
            exit_t = c.get("exit_time") or ("OPEN" if (is_open and not c.get("exit_combined")) else "")
            row += [c.get("entry_time", ""), c.get("entry_combined", ""),
                    exit_t, c.get("exit_combined", ""),
                    c.get("points", ""), c.get("pnl", "")]
        else:
            row += ["", "", "", "", "", ""]

    closed = [c for c in cycles if c.get("points") is not None]
    net_points = round(sum(c.get("points") or 0 for c in closed), 2)
    row += [
        len(closed),
        net_points,
        snap.get("realized_pnl", ""),
        snap.get("mtm_pnl", ""),
        "YES" if is_open else "",
        "YES" if snap.get("killed") else "",
        snap.get("kill_reason") or "",
        snap.get("updated", ""),
        dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]
    return row


def log_live_day(date_str: str = None, indices: list[str] = None):
    """Upsert the day's REAL live-tab result into the single 'Vwap Live Zerodha' tab —
    one row per index, keyed by (Date, Index). Reads the LIVE.json snapshots. Only the
    DTE 0/1 trade days are logged (other days are chart-only, no trades)."""
    date_str = date_str or dt.date.today().isoformat()
    indices = indices or ["NIFTY", "SENSEX"]
    ws = None
    logged = 0
    for idx in indices:
        snap = _load_live(date_str, idx)
        if not snap:
            print(f"  [sheets-live] no LIVE snapshot for {idx} {date_str} — skip")
            continue
        if snap.get("dte") not in (0, 1):
            print(f"  [sheets-live] {idx} {date_str} DTE={snap.get('dte')} — chart-only, not logged")
            continue
        if ws is None:
            ws = _open_named_ws(LIVE_WS_NAME, LIVE_HEADER)
        _upsert(ws, build_live_row(snap), key_idx=(0, 1), label=f"LIVE/{idx}")
        logged += 1
    print(f"  [sheets-live] done — {logged} row(s) into '{LIVE_WS_NAME}' for {date_str}")
    return logged


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Log Vwap-Strangle results to Google Sheets")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--index", default=None, help="NIFTY or SENSEX (default: both)")
    ap.add_argument("--auth", action="store_true", help="one-time browser auth (cache token)")
    ap.add_argument("--live", action="store_true",
                    help="log the REAL live-tab result (LIVE.json) into 'Vwap Live Zerodha'")
    a = ap.parse_args()
    idxs = [a.index.upper()] if a.index else None
    if a.auth:
        authorize()
    elif a.live:
        log_live_day(a.date, idxs)
    else:
        log_paper_day(a.date, idxs)
