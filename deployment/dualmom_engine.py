"""
dualmom_engine.py
DualMom.Liq.Nifty50 — signal computation, portfolio ranking, equity curve.
Monthly Nifty 500 momentum strategy with absolute filter (Nifty 100MA).
Strategy: top-50 stocks by 12m return, momentum-weighted, OUT = liquid fund (6% p.a.).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import time
import threading
from pathlib import Path
from datetime import datetime, date, timedelta
import pandas as pd
import numpy as np
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

ROOT              = Path(__file__).resolve().parent.parent
DATA_DIR          = Path(os.getenv("DATA_DIR", r"G:\fyers_data_pipeline\Nifty 500 Daily Data"))
EQUITY_CSV        = ROOT / "backtesting/book_strategies/antonacci/results/dual_momentum_v7_nav.csv"
ACCESS_TOKEN_PATH = Path(os.getenv("ACCESS_TOKEN_PATH", r"G:\fyers_data_pipeline\config\access_token.txt"))
APP_ID            = os.getenv("FYERS_APP_ID", "W09OMXQB8J-100")

LOOKBACK  = 252   # 12m
TOP_N     = 50
CAPITAL   = 1_000_000   # ₹10L paper notional
LIQUID_PA = 0.06        # 6% p.a. liquid fund

STRATEGY_STATS = {
    "cagr":     33.91,
    "sharpe":   1.590,
    "max_dd":  -15.1,
    "final_nav": 38_400_000,
    "years":    20,
    "capital":  1_000_000,
    "description": "Nifty 500 · Monthly · Top-50 Momentum · Nifty 100MA filter",
}

_lock  = threading.Lock()
_cache: dict = {}


# ── Nifty 50 via Fyers API ────────────────────────────────────────────────────

def _fetch_nifty() -> pd.Series | None:
    """Fetch Nifty 50 daily closes from Fyers API (last 400 calendar days)."""
    try:
        token_text = ACCESS_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if not token_text:
            print("  [dualmom] No access token — signal unavailable.")
            return None
        from fyers_apiv3 import fyersModel
        fyers = fyersModel.FyersModel(
            client_id=f"{APP_ID}:{token_text}",
            is_async=False,
            token=token_text,
            log_path="",
        )
        today      = date.today()
        start_ep   = int(time.mktime((today - timedelta(days=400)).timetuple()))
        end_ep     = int(time.mktime(today.timetuple()))
        resp = fyers.history({
            "symbol":      "NSE:NIFTY50-INDEX",
            "resolution":  "D",
            "date_format": "1",
            "range_from":  str(start_ep),
            "range_to":    str(end_ep),
            "cont_flag":   "1",
        })
        if resp.get("s") != "ok":
            print(f"  [dualmom] Nifty fetch failed: {resp.get('message')}")
            return None
        candles = resp["candles"]  # [[epoch, o, h, l, c, v], ...]
        df = pd.DataFrame(candles, columns=["ts", "o", "h", "l", "c", "v"])
        df["date"] = (
            pd.to_datetime(df["ts"], unit="s", utc=True)
            .dt.tz_convert("Asia/Kolkata")
            .dt.normalize()
            .dt.tz_localize(None)
        )
        s = df.set_index("date")["c"].sort_index().dropna()
        print(f"  [dualmom] Nifty loaded: {len(s)} days, latest={float(s.iloc[-1]):.0f}")
        return s
    except Exception as e:
        print(f"  [dualmom] Nifty fetch error: {e}")
        return None


# ── Nifty 500 parquets ────────────────────────────────────────────────────────

def _load_nifty500() -> pd.DataFrame | None:
    """Load all Nifty 500 daily close prices from DATA_DIR parquet files."""
    frames = {}
    for f in DATA_DIR.glob("*.parquet"):
        try:
            df = pd.read_parquet(f, columns=["close"])
            df.index = pd.to_datetime(df.index)
            frames[f.stem] = df["close"]
        except Exception:
            pass
    if not frames:
        print("  [dualmom] No Nifty 500 parquets found in DATA_DIR.")
        return None
    prices = pd.DataFrame(frames).sort_index()
    print(f"  [dualmom] Nifty500 prices: {prices.shape[0]} days × {prices.shape[1]} stocks")
    return prices


# ── signal ────────────────────────────────────────────────────────────────────

def _compute_signal(nifty: pd.Series | None) -> dict:
    if nifty is None or len(nifty) < 101:
        return {
            "signal": "UNKNOWN", "nifty_price": 0.0,
            "ma100": 0.0, "gap_pct": 0.0, "as_of": "",
        }
    ma100 = nifty.rolling(100).mean()
    px    = float(nifty.iloc[-1])
    ma    = float(ma100.iloc[-1])
    gap   = (px - ma) / ma * 100
    return {
        "signal":      "IN" if px > ma else "OUT",
        "nifty_price": round(px, 2),
        "ma100":       round(ma, 2),
        "gap_pct":     round(gap, 2),
        "as_of":       nifty.index[-1].strftime("%Y-%m-%d"),
    }


# ── portfolio ─────────────────────────────────────────────────────────────────

def _compute_portfolio(prices: pd.DataFrame | None, signal: dict) -> list:
    if prices is None or signal["signal"] != "IN":
        return []
    n  = len(prices) - 1
    lb = n - LOOKBACK
    if lb < 0:
        return []

    cur  = prices.iloc[n]
    past = prices.iloc[lb]
    ret  = (cur / past - 1).dropna()
    top  = ret.nlargest(TOP_N)

    raw   = {s: max(r, 0.001) for s, r in top.items()}
    total = sum(raw.values())
    wts   = {s: v / total for s, v in raw.items()}

    portfolio = []
    for rank, (sym, w) in enumerate(wts.items(), 1):
        px = float(cur.get(sym, 0))
        if px <= 0:
            continue
        shares = (CAPITAL * w) / px
        portfolio.append({
            "rank":        rank,
            "sym":         sym,
            "weight":      round(w * 100, 2),
            "entry_price": round(px, 2),
            "shares":      round(shares, 4),
            "mom_return":  round(float(top[sym]) * 100, 2),
            "allocated":   round(CAPITAL * w, 0),
        })
    return portfolio


# ── equity curve ──────────────────────────────────────────────────────────────

def _load_equity() -> list:
    """Load backtest equity curve (column B = momentum weighted) from CSV."""
    try:
        df  = pd.read_csv(EQUITY_CSV, index_col=0, parse_dates=True)
        col = "B" if "B" in df.columns else df.columns[0]
        nav = df[col].dropna()
        return [{"date": str(d.date()), "nav": round(float(v), 0)} for d, v in nav.items()]
    except Exception as e:
        print(f"  [dualmom] Equity CSV error: {e}")
        return []


# ── public API ────────────────────────────────────────────────────────────────

def refresh() -> None:
    """Reload signal + portfolio + equity. Call at startup and EOD."""
    print("  [dualmom] Refreshing...")
    nifty     = _fetch_nifty()
    prices    = _load_nifty500()
    signal    = _compute_signal(nifty)
    portfolio = _compute_portfolio(prices, signal)
    equity    = _load_equity()

    with _lock:
        _cache["signal"]       = signal
        _cache["portfolio"]    = portfolio
        _cache["equity"]       = equity
        _cache["last_updated"] = datetime.now().isoformat()

    print(f"  [dualmom] Done — Signal:{signal['signal']}  Portfolio:{len(portfolio)} stocks")


def get_signal() -> dict:
    with _lock:
        return dict(_cache.get("signal", {}))


def get_portfolio() -> list:
    with _lock:
        return list(_cache.get("portfolio", []))


def get_equity() -> list:
    with _lock:
        return list(_cache.get("equity", []))


def get_live_pnl(live_prices: dict) -> dict:
    """Compute unrealized P&L for current portfolio using live/last-close prices."""
    signal    = get_signal()
    portfolio = get_portfolio()

    if signal.get("signal") != "IN" or not portfolio:
        return {
            "signal":        signal,
            "status":        "out",
            "positions":     [],
            "total_pnl":     0,
            "total_pnl_pct": 0.0,
            "total_value":   CAPITAL,
            "total_cost":    CAPITAL,
        }

    positions   = []
    total_cost  = 0.0
    total_value = 0.0

    for pos in portfolio:
        sym         = pos["sym"]
        entry_price = pos["entry_price"]
        shares      = pos["shares"]
        live_price  = live_prices.get(sym, entry_price)
        if not live_price or live_price <= 0:
            live_price = entry_price
        cost  = entry_price * shares
        value = live_price  * shares
        pnl   = value - cost
        pnl_p = (live_price / entry_price - 1) * 100 if entry_price > 0 else 0.0
        total_cost  += cost
        total_value += value
        positions.append({
            **pos,
            "live_price":    round(live_price, 2),
            "current_value": round(value, 0),
            "pnl":           round(pnl, 0),
            "pnl_pct":       round(pnl_p, 2),
        })

    positions.sort(key=lambda x: x["pnl"], reverse=True)
    total_pnl   = total_value - total_cost
    total_pnl_p = total_pnl / total_cost * 100 if total_cost > 0 else 0.0

    return {
        "signal":        signal,
        "status":        "in",
        "positions":     positions,
        "total_pnl":     round(total_pnl, 0),
        "total_pnl_pct": round(total_pnl_p, 2),
        "total_value":   round(total_value, 0),
        "total_cost":    round(total_cost, 0),
    }


def get_stats() -> dict:
    return STRATEGY_STATS


def get_last_updated() -> str:
    with _lock:
        return _cache.get("last_updated", "")
