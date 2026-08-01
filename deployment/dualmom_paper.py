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

import logging
from logging.handlers import RotatingFileHandler as _RFH

ROOT = Path(__file__).resolve().parent

def _make_logger():
    log_dir = ROOT.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    h = _RFH(log_dir / "dualmom_paper.log", maxBytes=2*1024*1024, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s"))
    log = logging.getLogger("dualmom_paper")
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.addHandler(h)
    return log

_log = _make_logger()

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


def _fetch_quotes_batch(fyers, symbols: list[str]) -> dict[str, float]:
    """Fetch LTP for up to 50 symbols in a single Quotes API call.
    Returns {sym: ltp} dict. Symbols should be plain NSE names (no prefix)."""
    if not symbols:
        return {}
    try:
        syms_str = ",".join(f"NSE:{s}-EQ" for s in symbols)
        resp = fyers.quotes({"symbols": syms_str})
        if resp.get("s") != "ok":
            print(f"  [paper] quotes API error: {resp.get('message')}")
            return {}
        result = {}
        for item in resp.get("d", []):
            raw_sym = item.get("n", "")          # "NSE:TCS-EQ"
            ltp     = item.get("v", {}).get("lp")
            if raw_sym and ltp:
                sym = raw_sym.replace("NSE:", "").replace("-EQ", "")
                result[sym] = float(ltp)
        return result
    except Exception as e:
        print(f"  [paper] quotes batch error: {e}")
        return {}


def _fetch_returns_12m_batch(symbols: list[str]) -> dict[str, tuple]:
    """Fetch 12m returns for all symbols via yfinance batch download.
    Returns {sym: (current_price, price_252_ago)} for valid symbols."""
    import yfinance as yf
    import pandas as pd
    ns_syms = [f"{s}.NS" for s in symbols]
    try:
        raw = yf.download(ns_syms, period="15mo", auto_adjust=True, progress=False)
        closes = raw["Close"] if "Close" in raw.columns else raw
        if isinstance(closes, pd.Series):
            closes = closes.to_frame()
        results = {}
        for sym, ns in zip(symbols, ns_syms):
            col = ns if ns in closes.columns else sym
            if col not in closes.columns:
                continue
            series = closes[col].dropna()
            if len(series) >= LOOKBACK:
                results[sym] = (float(series.iloc[-1]), float(series.iloc[-LOOKBACK]))
        return results
    except Exception as e:
        _log.error(f"yfinance batch fetch error: {e}")
        return {}


def _get_nifty_signal(_fyers=None) -> tuple:
    """Returns (signal_str, nifty_px, ma100). Uses yfinance — no Fyers History API."""
    import yfinance as yf
    import pandas as pd
    nifty_raw = yf.download("^NSEI", period="200d", auto_adjust=True, progress=False)
    nifty = nifty_raw["Close"].squeeze()
    if isinstance(nifty, pd.DataFrame):
        nifty = nifty.iloc[:, 0]
    nifty.index = pd.to_datetime(nifty.index).tz_localize(None)
    if len(nifty) < 100:
        raise ValueError(f"Not enough Nifty data: {len(nifty)} days")
    ma100    = float(nifty.rolling(100).mean().iloc[-1])
    nifty_px = float(nifty.iloc[-1])
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

def _backfill_liquid_days(state: dict, eq: dict):
    """Fill any calendar days missing from equity history when status=out.
    Only safe to call while holding _lock and only for liquid-fund periods."""
    history    = eq["history"]
    entry_nav  = float(state["liquid_fund_entry_nav"])
    entry_date = date.fromisoformat(state["liquid_fund_entry_date"])
    existing   = {h["date"] for h in history}
    today      = date.today()

    d = (date.fromisoformat(history[-1]["date"]) + timedelta(days=1)) if history else entry_date
    while d < today:
        ds = d.isoformat()
        if ds not in existing:
            days_in  = (d - entry_date).days
            days_pre = days_in - 1
            nav_d    = entry_nav * ((1 + LIQUID_PA / 365) ** days_in)
            nav_p    = entry_nav * ((1 + LIQUID_PA / 365) ** days_pre)
            ret      = nav_d - nav_p
            ret_pct  = round(ret / nav_p * 100, 4) if nav_p > 0 else 0.0
            history.append({
                "date":              ds,
                "nav":               round(nav_d, 2),
                "status":            "out",
                "liquid_return_abs": round(ret, 2),
                "liquid_return_pct": ret_pct,
                "stock_return_abs":  0.0,
                "stock_return_pct":  0.0,
                "total_return_abs":  round(ret, 2),
                "total_return_pct":  ret_pct,
            })
            print(f"  [paper] Backfilled missing day: {ds} NAV=₹{nav_d:,.0f}")
        d += timedelta(days=1)

    # keep history sorted
    eq["history"] = sorted(history, key=lambda x: x["date"])


def record_daily_nav():
    """Called at 16:00 daily. Fetches EOD prices, records NAV and daily returns."""
    today_str = date.today().isoformat()
    _log.info(f"record_daily_nav called for {today_str}")

    with _lock:
        state = _load_paper()
        eq    = _load_equity()

        # Auto-backfill any missed calendar days (liquid fund only — safe, math only)
        if state["status"] == "out" and eq["history"]:
            _backfill_liquid_days(state, eq)
            _save_equity(eq)

    if eq["history"] and eq["history"][-1]["date"] == today_str:
        _log.info(f"NAV already recorded for {today_str} — skipping.")
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
        _log.info(f"Liquid fund: NAV=₹{today_nav:,.0f} (+₹{liquid_abs:,.0f})")

    elif state["status"] == "in" and state["holdings"]:
        try:
            fyers      = _get_fyers()
            syms       = [h["sym"] for h in state["holdings"]]
            prices     = _fetch_quotes_batch(fyers, syms)
            total_val  = 0.0
            failed     = 0
            for h in state["holdings"]:
                sym   = h["sym"]
                price = prices.get(sym)
                if price and price > 0:
                    h["current_price"] = round(price, 2)
                    total_val += price * h["shares"]
                else:
                    total_val += h["entry_price"] * h["shares"]
                    failed += 1
            today_nav  = total_val
            stock_abs  = today_nav - prev_nav
            _log.info(f"Stocks NAV: ₹{today_nav:,.0f} ({stock_abs:+,.0f}) [quotes API, {failed} failed]")
        except Exception as e:
            _log.error(f"EOD stock price error: {e}")
    else:
        _log.warning(f"Status={state['status']} — no NAV to record.")
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

    _log.info(f"NAV recorded: ₹{today_nav:,.0f} | day {total_pct:+.3f}%")


# ── Month-end rebalance ───────────────────────────────────────────────────────

def run_month_end_rebalance():
    """Called on last trading day of month (after record_daily_nav).
    Fetches 500-stock returns via Fyers API, picks top-50, updates holdings."""
    today_str = date.today().isoformat()
    _log.info(f"=== Month-end rebalance START: {today_str} ===")

    try:
        fyers = _get_fyers()
        nifty_signal, nifty_px, ma100 = _get_nifty_signal(fyers)
    except Exception as e:
        _log.error(f"Rebalance ABORTED — Nifty fetch failed: {e}")
        return

    _log.info(f"Nifty={nifty_px:.0f}  100MA={ma100:.0f}  Signal={nifty_signal}")

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
        _log.info(f"Signal OUT — NAV=₹{current_nav:,.0f} → liquid fund. Rebalance done.")
        return

    # Signal IN — fetch 12m returns for all 500 stocks via yfinance batch
    from deployment.nifty500_symbols import NIFTY500
    _log.info(f"Signal IN — fetching 12m returns for {len(NIFTY500)} stocks via yfinance...")

    batch_results = _fetch_returns_12m_batch(NIFTY500)
    _log.info(f"yfinance batch: {len(batch_results)} valid symbols out of {len(NIFTY500)}")

    returns = {}
    prices  = {}
    for sym, (cur, past) in batch_results.items():
        if cur and past and past > 0:
            returns[sym] = cur / past - 1
            prices[sym]  = cur

    if len(returns) < 10:
        _log.error(f"Rebalance ABORTED — too few valid symbols ({len(returns)})")
        return

    sorted_ret = sorted(returns.items(), key=lambda x: x[1], reverse=True)
    top50      = sorted_ret[:TOP_N]

    raw   = {s: max(r, 0.001) for s, r in top50}
    total = sum(raw.values())
    wts   = {s: v / total for s, v in raw.items()}

    # Fetch real entry prices via Fyers Quotes API (replaces yfinance-adjusted prices)
    top50_syms = [s for s, _ in top50]
    quote_prices = _fetch_quotes_batch(fyers, top50_syms)
    _log.info(f"Quotes API entry prices: {len(quote_prices)} of {len(top50_syms)} fetched")

    holdings = []
    for rank, (sym, w) in enumerate(wts.items(), 1):
        px = quote_prices.get(sym) or prices.get(sym, 0.0)
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
        state["liquid_fund_entry_nav"]  = None
        state["liquid_fund_entry_date"] = None
        _save_paper(state)

    _log.info(f"=== Rebalance DONE — IN: {len(holdings)} stocks, NAV=₹{current_nav:,.0f} ===")


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
    _log.info(f"Initialized — status={state['status']}  NAV=₹{state['current_nav']:,.0f}")


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


def get_live_quotes() -> dict:
    """Fetch current LTP for all held stocks via Quotes API. Called on demand."""
    with _lock:
        state = _load_paper()
    if state["status"] != "in" or not state["holdings"]:
        return {"status": state["status"], "quotes": [], "error": None}
    try:
        fyers  = _get_fyers()
        syms   = [h["sym"] for h in state["holdings"]]
        prices = _fetch_quotes_batch(fyers, syms)
        quotes = []
        total_val  = 0.0
        total_cost = 0.0
        for h in state["holdings"]:
            sym        = h["sym"]
            ltp        = prices.get(sym, h["entry_price"])
            entry      = h["entry_price"]
            shares     = h["shares"]
            val        = ltp * shares
            cost       = entry * shares
            pnl        = val - cost
            pnl_pct    = (ltp / entry - 1) * 100 if entry > 0 else 0.0
            total_val  += val
            total_cost += cost
            quotes.append({
                **h,
                "ltp":          round(ltp, 2),
                "current_value": round(val, 0),
                "pnl":          round(pnl, 0),
                "pnl_pct":      round(pnl_pct, 2),
            })
        quotes.sort(key=lambda x: x["pnl_pct"], reverse=True)
        total_pnl   = total_val - total_cost
        total_pnl_p = total_pnl / total_cost * 100 if total_cost > 0 else 0.0
        return {
            "status":        "in",
            "quotes":        quotes,
            "total_value":   round(total_val, 0),
            "total_pnl":     round(total_pnl, 0),
            "total_pnl_pct": round(total_pnl_p, 2),
            "error":         None,
        }
    except Exception as e:
        return {"status": "in", "quotes": [], "error": str(e)}
