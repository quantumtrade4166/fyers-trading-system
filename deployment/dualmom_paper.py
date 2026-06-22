"""
dualmom_paper.py
DualMom.Liq.Nifty50 — paper trading engine.
  - ₹10L starting capital
  - Daily NAV recorded at 16:00 via Fyers API (held stocks) or compound interest (liquid fund)
  - Month-end rebalance: Fyers History API for all 500 stocks (500 calls, ~2 min)
  - Signal log for each month-end flip
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import time
import threading
from pathlib import Path
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT = Path(__file__).resolve().parent

ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", r"G:\fyers_data_pipeline\config\access_token.txt"))
APP_ID            = os.getenv("FYERS_APP_ID", "W09OMXQB8J-100")

PAPER_JSON  = ROOT / "dualmom_paper.json"
EQUITY_JSON = ROOT / "dualmom_paper_equity.json"
SIGNAL_JSON = ROOT / "dualmom_signal_log.json"

START_CAPITAL    = 1_000_000
LIQUID_PA        = 0.06
TOP_N            = 50
LOOKBACK         = 252
RATE_LIMIT_SLEEP = 0.15   # seconds between Fyers history calls

_lock = threading.Lock()


# ── Fyers API helper ──────────────────────────────────────────────────────────

def _get_fyers():
    raw = ACCESS_TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError("Empty access token")
    try:
        import json as _json
        payload = _json.loads(raw)
        token   = payload["token"]
    except Exception:
        token = raw
    from fyers_apiv3 import fyersModel
    return fyersModel.FyersModel(
        client_id=f"{APP_ID}:{token}",
        is_async=False,
        token=token,
        log_path="",
    )


def _fetch_close(fyers, sym: str) -> float | None:
    """Fetch the most recent daily close for a symbol."""
    try:
        today    = date.today()
        start_ep = int(time.mktime((today - timedelta(days=7)).timetuple()))
        end_ep   = int(time.mktime(today.timetuple()))
        resp = fyers.history({
            "symbol":      f"NSE:{sym}-EQ",
            "resolution":  "D",
            "date_format": "1",
            "range_from":  str(start_ep),
            "range_to":    str(end_ep),
            "cont_flag":   "1",
        })
        if resp.get("s") == "ok" and resp.get("candles"):
            return float(resp["candles"][-1][4])
    except Exception as e:
        print(f"  [paper] fetch_close error {sym}: {e}")
    return None


def _fetch_return_12m(fyers, sym: str) -> tuple:
    """Fetch (current_price, price_252_days_ago). Returns (None, None) on failure."""
    try:
        today    = date.today()
        start_ep = int(time.mktime((today - timedelta(days=380)).timetuple()))
        end_ep   = int(time.mktime(today.timetuple()))
        resp = fyers.history({
            "symbol":      f"NSE:{sym}-EQ",
            "resolution":  "D",
            "date_format": "1",
            "range_from":  str(start_ep),
            "range_to":    str(end_ep),
            "cont_flag":   "1",
        })
        if resp.get("s") == "ok" and resp.get("candles"):
            candles = resp["candles"]
            if len(candles) >= LOOKBACK:
                return float(candles[-1][4]), float(candles[-LOOKBACK][4])
    except Exception as e:
        print(f"  [paper] fetch_12m error {sym}: {e}")
    return None, None


def _get_nifty_signal(fyers) -> tuple:
    """Returns (signal_str, nifty_px, ma100)."""
    today    = date.today()
    start_ep = int(time.mktime((today - timedelta(days=180)).timetuple()))
    end_ep   = int(time.mktime(today.timetuple()))
    resp = fyers.history({
        "symbol":      "NSE:NIFTY50-INDEX",
        "resolution":  "D",
        "date_format": "1",
        "range_from":  str(start_ep),
        "range_to":    str(end_ep),
        "cont_flag":   "1",
    })
    if resp.get("s") != "ok" or not resp.get("candles"):
        raise ValueError(f"Nifty fetch failed: {resp.get('message')}")
    closes = [c[4] for c in resp["candles"]]
    if len(closes) < 100:
        raise ValueError(f"Not enough Nifty data: {len(closes)} days")
    ma100    = sum(closes[-100:]) / 100
    nifty_px = closes[-1]
    return ("IN" if nifty_px > ma100 else "OUT"), nifty_px, ma100


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _load_paper() -> dict:
    if PAPER_JSON.exists():
        return json.loads(PAPER_JSON.read_text(encoding="utf-8"))
    return {
        "status":                  "unknown",
        "start_date":              date.today().isoformat(),
        "start_capital":           START_CAPITAL,
        "current_nav":             START_CAPITAL,
        "rebal_date":              None,
        "holdings":                [],
        "liquid_fund_entry_nav":   START_CAPITAL,
        "liquid_fund_entry_date":  date.today().isoformat(),
    }


def _save_paper(state: dict):
    PAPER_JSON.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_equity() -> dict:
    if EQUITY_JSON.exists():
        return json.loads(EQUITY_JSON.read_text(encoding="utf-8"))
    return {"start_capital": START_CAPITAL, "history": []}


def _save_equity(eq: dict):
    EQUITY_JSON.write_text(json.dumps(eq, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_signal_log() -> dict:
    if SIGNAL_JSON.exists():
        return json.loads(SIGNAL_JSON.read_text(encoding="utf-8"))
    return {"log": []}


def _save_signal_log(sl: dict):
    SIGNAL_JSON.write_text(json.dumps(sl, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Month-end detection ───────────────────────────────────────────────────────

def is_last_trading_day() -> bool:
    """True if today is the last business day of the current month."""
    today = date.today()
    if today.weekday() >= 5:
        return False
    next_day = today + timedelta(days=1)
    while next_day.weekday() >= 5:
        next_day += timedelta(days=1)
    return next_day.month != today.month


def _next_rebalance_date() -> str:
    """Last business day of the current month."""
    today = date.today()
    if today.month == 12:
        last = date(today.year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(today.year, today.month + 1, 1) - timedelta(days=1)
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return last.isoformat()


# ── Daily NAV ────────────────────────────────────────────────────────────────

def record_daily_nav():
    """Called at 16:00 daily. Fetches EOD prices, records NAV and daily returns."""
    today_str = date.today().isoformat()
    print(f"  [paper] Recording daily NAV for {today_str}...")

    with _lock:
        state = _load_paper()
        eq    = _load_equity()

    if eq["history"] and eq["history"][-1]["date"] == today_str:
        print(f"  [paper] NAV already recorded for {today_str}. Skipping.")
        return

    prev_nav   = state["current_nav"]
    today_nav  = float(prev_nav)
    liquid_abs = stock_abs = 0.0

    if state["status"] == "out":
        entry_nav   = float(state["liquid_fund_entry_nav"])
        entry_date  = date.fromisoformat(state["liquid_fund_entry_date"])
        days_in     = max(0, (date.today() - entry_date).days)
        today_nav   = entry_nav * ((1 + LIQUID_PA / 365) ** days_in)
        liquid_abs  = today_nav - prev_nav
        print(f"  [paper] Liquid fund: ₹{today_nav:,.0f} (+₹{liquid_abs:,.0f})")

    elif state["status"] == "in" and state["holdings"]:
        try:
            fyers       = _get_fyers()
            total_val   = 0.0
            failed      = 0
            for h in state["holdings"]:
                sym   = h["sym"]
                price = _fetch_close(fyers, sym)
                time.sleep(RATE_LIMIT_SLEEP)
                if price and price > 0:
                    h["current_price"] = round(price, 2)
                    total_val += price * h["shares"]
                else:
                    total_val += h["entry_price"] * h["shares"]
                    failed += 1
            today_nav  = total_val
            stock_abs  = today_nav - prev_nav
            print(f"  [paper] Stocks NAV: ₹{today_nav:,.0f} ({stock_abs:+,.0f}) [{failed} failed]")
        except Exception as e:
            print(f"  [paper] EOD stock price error: {e}")
    else:
        # unknown/uninitialised state — nothing to record
        print(f"  [paper] Status={state['status']} — no NAV to record.")
        return

    liquid_pct = (liquid_abs / prev_nav * 100) if prev_nav > 0 else 0.0
    stock_pct  = (stock_abs  / prev_nav * 100) if prev_nav > 0 else 0.0
    total_abs  = liquid_abs + stock_abs
    total_pct  = (total_abs  / prev_nav * 100) if prev_nav > 0 else 0.0

    with _lock:
        state["current_nav"] = round(today_nav, 2)
        _save_paper(state)
        eq["history"].append({
            "date":              today_str,
            "nav":               round(today_nav, 2),
            "status":            state["status"],
            "liquid_return_abs": round(liquid_abs, 2),
            "liquid_return_pct": round(liquid_pct, 4),
            "stock_return_abs":  round(stock_abs, 2),
            "stock_return_pct":  round(stock_pct, 4),
            "total_return_abs":  round(total_abs, 2),
            "total_return_pct":  round(total_pct, 4),
        })
        _save_equity(eq)

    print(f"  [paper] NAV recorded: ₹{today_nav:,.0f} | day {total_pct:+.3f}%")


# ── Month-end rebalance ───────────────────────────────────────────────────────

def run_month_end_rebalance():
    """Called on last trading day of month (after record_daily_nav).
    Fetches 500-stock returns via Fyers API, picks top-50, updates holdings."""
    today_str = date.today().isoformat()
    print(f"  [paper] Month-end rebalance: {today_str}")

    try:
        fyers = _get_fyers()
        nifty_signal, nifty_px, ma100 = _get_nifty_signal(fyers)
    except Exception as e:
        print(f"  [paper] Rebalance aborted — Nifty error: {e}")
        return

    print(f"  [paper] Nifty={nifty_px:.0f}  100MA={ma100:.0f}  Signal={nifty_signal}")

    with _lock:
        sl = _load_signal_log()
        sl["log"].append({
            "date":        today_str,
            "signal":      nifty_signal,
            "nifty_price": round(nifty_px, 2),
            "ma100":       round(ma100, 2),
            "gap_pct":     round((nifty_px - ma100) / ma100 * 100, 2),
        })
        _save_signal_log(sl)
        state = _load_paper()

    current_nav = float(state["current_nav"])

    if nifty_signal == "OUT":
        with _lock:
            state["status"]                 = "out"
            state["holdings"]               = []
            state["rebal_date"]             = today_str
            state["liquid_fund_entry_nav"]  = current_nav
            state["liquid_fund_entry_date"] = today_str
            _save_paper(state)
        print(f"  [paper] Signal OUT — ₹{current_nav:,.0f} → liquid fund.")
        return

    # Signal IN — fetch 12m returns for all 500 stocks
    from deployment.nifty500_symbols import NIFTY500
    print(f"  [paper] Signal IN — fetching returns for {len(NIFTY500)} stocks...")

    returns = {}
    prices  = {}
    for i, sym in enumerate(NIFTY500):
        cur, past = _fetch_return_12m(fyers, sym)
        time.sleep(RATE_LIMIT_SLEEP)
        if cur and past and past > 0:
            returns[sym] = cur / past - 1
            prices[sym]  = cur
        if (i + 1) % 100 == 0:
            print(f"  [paper] Progress: {i+1}/{len(NIFTY500)} ({len(returns)} valid)")

    if len(returns) < 10:
        print(f"  [paper] Rebalance aborted — too few valid symbols ({len(returns)})")
        return

    sorted_ret = sorted(returns.items(), key=lambda x: x[1], reverse=True)
    top50      = sorted_ret[:TOP_N]

    raw   = {s: max(r, 0.001) for s, r in top50}
    total = sum(raw.values())
    wts   = {s: v / total for s, v in raw.items()}

    holdings = []
    for rank, (sym, w) in enumerate(wts.items(), 1):
        px = prices.get(sym, 0.0)
        if px <= 0:
            continue
        shares = (current_nav * w) / px
        holdings.append({
            "rank":          rank,
            "sym":           sym,
            "weight":        round(w * 100, 2),
            "entry_price":   round(px, 2),
            "current_price": round(px, 2),
            "shares":        round(shares, 4),
            "mom_return":    round(returns[sym] * 100, 2),
            "allocated":     round(current_nav * w, 0),
        })

    with _lock:
        state["status"]    = "in"
        state["holdings"]  = holdings
        state["rebal_date"]= today_str
        _save_paper(state)

    print(f"  [paper] Rebalance done — IN: {len(holdings)} stocks, NAV=₹{current_nav:,.0f}")


# ── Public API ────────────────────────────────────────────────────────────────

def init():
    """Create JSON files if they don't exist. Call at app startup."""
    with _lock:
        state = _load_paper()
        eq    = _load_equity()
        sl    = _load_signal_log()
        _save_paper(state)
        _save_equity(eq)
        _save_signal_log(sl)
    print(f"  [paper] Initialized — status={state['status']}  NAV=₹{state['current_nav']:,.0f}")


def get_paper_state() -> dict:
    with _lock:
        state = _load_paper()
    start_cap   = float(state["start_capital"])
    current_nav = float(state["current_nav"])
    total_pnl   = current_nav - start_cap
    total_pnl_p = (total_pnl / start_cap * 100) if start_cap > 0 else 0.0

    # liquid fund daily accrual
    liquid_daily_abs = 0.0
    if state["status"] == "out":
        entry_nav  = float(state["liquid_fund_entry_nav"])
        liquid_daily_abs = entry_nav * (LIQUID_PA / 365)

    return {
        "status":            state["status"],
        "start_date":        state["start_date"],
        "start_capital":     start_cap,
        "current_nav":       round(current_nav, 2),
        "total_pnl":         round(total_pnl, 2),
        "total_pnl_pct":     round(total_pnl_p, 4),
        "rebal_date":        state.get("rebal_date"),
        "next_rebal_date":   _next_rebalance_date(),
        "holdings":          state.get("holdings", []),
        "holdings_count":    len(state.get("holdings", [])),
        "liquid_daily_abs":  round(liquid_daily_abs, 2),
    }


def get_paper_equity() -> dict:
    with _lock:
        return _load_equity()


def get_signal_log() -> list:
    with _lock:
        sl = _load_signal_log()
    return sl.get("log", [])
