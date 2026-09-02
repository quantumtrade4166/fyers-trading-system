"""
main.py
FastAPI application — REST API + WebSocket push to browser.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from deployment.scheduler import create_scheduler
from deployment import signal_engine, positions as pos_store, live_feed, dualmom_engine, dualmom_paper

MODE = os.getenv("TRADING_MODE", "paper").upper()

_scheduler      = None
_ws_clients: list[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    print(f"\n  ── Pairs Dashboard starting ({MODE} mode) ──")

    # ── Single-instance guard ────────────────────────────────────────────────
    # The VPS venv double-launches this app under the system python. Two app
    # instances = two schedulers = duplicate captures AND duplicate ORDERS. Only
    # the instance that wins the lock runs the scheduler / live feed / order
    # routing; the duplicate serves the web UI read-only. (See core/singleton.py.)
    import sys as _sys
    from pathlib import Path as _Path
    _sp = str(_Path(__file__).parent.parent / "live_trading_options" / "strangle_strategy")
    if _sp not in _sys.path:
        _sys.path.append(_sp)
    try:
        from core.singleton import acquire, PORT_SCHEDULER
        _is_primary = acquire(PORT_SCHEDULER)
    except Exception as e:
        print(f"  [main] singleton check failed ({e}); assuming primary")
        _is_primary = True

    signal_engine.init_engine()
    dualmom_engine.refresh()
    dualmom_paper.init()

    if _is_primary:
        _scheduler = create_scheduler()
        _scheduler.start()
        print("  [main] PRIMARY instance — scheduler started.")

        # ensure today's Zerodha token exists (in case server starts after 08:50)
        try:
            from deployment.brokers import zerodha_auto_login
            zerodha_auto_login.ensure_token()
        except Exception as e:
            print(f"  [main] Zerodha ensure_token error: {e}")

        # if server starts during market hours, kick off the live feed immediately
        import pytz
        from datetime import datetime
        _ist = pytz.timezone("Asia/Kolkata")
        _now = datetime.now(_ist)
        _market_open = _now.weekday() < 5 and (9, 15) <= (_now.hour, _now.minute) <= (15, 30)
        if _market_open:
            print("  [main] Market is open — starting live feed immediately.")
            live_feed.start_feed()
    else:
        print("  [main] DUPLICATE instance detected — scheduler/feed/orders DISABLED "
              "(passive web only). Fix the venv to remove the duplicate entirely.")

    asyncio.create_task(_push_loop())
    yield

    if _scheduler:
        _scheduler.shutdown(wait=False)
        live_feed.stop_feed()
    print("  [main] Shutdown complete.")


app = FastAPI(title="Pairs Dashboard", lifespan=lifespan)

# gzip JSON/HTML responses — big win over the Tailscale tunnel (payloads shrink 5-10x).
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── tiny thread-safe TTL cache ───────────────────────────────────────────────
# The pair-signal compute is ~0.5-3s. Running it on the event loop froze EVERY request
# (a 16-byte /api/version took 2.3s). We (a) run it in a thread so it never blocks the loop
# and (b) cache the result for a couple of seconds so rapid polls/loads reuse it instead of
# recomputing. Read-only — no trading logic here. Per-key lock prevents a thundering herd.
import threading as _threading
import time as _time
_cache_store: dict = {}                      # key -> (expiry_monotonic, value)
_cache_glock = _threading.Lock()
_cache_klocks: dict = {}                     # key -> Lock


def _ttl_cached(key: str, ttl: float, producer):
    now = _time.monotonic()
    with _cache_glock:
        hit = _cache_store.get(key)
        if hit and hit[0] > now:
            return hit[1]
        klock = _cache_klocks.setdefault(key, _threading.Lock())
    with klock:                              # only ONE thread computes a given key at a time
        now = _time.monotonic()
        hit = _cache_store.get(key)
        if hit and hit[0] > now:             # filled while we waited for the lock
            return hit[1]
        val = producer()
        with _cache_glock:
            _cache_store[key] = (now + ttl, val)
        return val


def _signals_cached():
    """Pair signals, computed at most once per ~2s and off the event loop."""
    return _ttl_cached("signals", 2.0,
                       lambda: signal_engine.get_all_signals(live_feed.get_live_prices() or None))


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    # no-cache => the browser REVALIDATES index.html on every load (304 if unchanged), so a
    # deploy is picked up without a manual hard-refresh. Stale cached JS had silently
    # re-introduced already-fixed bugs (e.g. the live ticker vanishing, GO LIVE disabled).
    return FileResponse(str(STATIC_DIR / "index.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/version")
async def api_version():
    """Build id = index.html mtime. The page polls this and RELOADS itself when it changes,
    so a tab left open across a deploy self-heals to the latest JS instead of silently running
    stale code (the recurring root cause of 'the live ticker vanished again')."""
    try:
        return {"v": int((STATIC_DIR / "index.html").stat().st_mtime)}
    except Exception:
        return {"v": 0}


@app.get("/api/status")
async def api_status():
    return {
        "mode":        MODE,
        "feed_active": live_feed.is_running(),
        "live_prices": len(live_feed.get_live_prices()),
    }


@app.get("/api/signals")
async def api_signals():
    # off the event loop + cached ~2s, so this heavy compute never freezes other requests
    return await asyncio.to_thread(_signals_cached)


@app.get("/api/positions")
async def api_positions():
    return await asyncio.to_thread(_positions_computed)


def _positions_computed():
    positions = pos_store.get_positions()
    live_prices = live_feed.get_live_prices() or {}
    eod_snap    = live_feed.get_eod_snapshot()
    signals = _signals_cached()

    for name, pos in positions.items():
        sig   = signals.get(name, {})
        sym_a = pos.get("sym_a", "")
        sym_b = pos.get("sym_b", "")
        # priority: live feed → Fyers EOD snapshot (15:30 close) → parquet via signal
        cur_a = (live_prices.get(sym_a)
                 or eod_snap.get(sym_a)
                 or sig.get("price_a")
                 or pos["entry_price_a"])
        cur_b = (live_prices.get(sym_b)
                 or eod_snap.get(sym_b)
                 or sig.get("price_b")
                 or pos["entry_price_b"])
        qty_a = pos["qty_a"]
        qty_b = pos["qty_b"]
        sign  = 1 if pos["direction"] == "long_spread" else -1
        gross = ((cur_a - pos["entry_price_a"]) * qty_a
                 - (cur_b - pos["entry_price_b"]) * qty_b) * sign
        cost  = (pos["entry_price_a"] * qty_a + pos["entry_price_b"] * qty_b
                 + cur_a * qty_a + cur_b * qty_b) * 0.0003
        pos["unrealised_pnl"] = round(gross - cost, 2)
        pos["current_price_a"] = round(float(cur_a), 2)
        pos["current_price_b"] = round(float(cur_b), 2)
        pos["current_z"] = sig.get("z")

    return positions


@app.get("/api/trades")
async def api_trades(limit: int = 50):
    return pos_store.get_trades(limit)


@app.get("/api/equity")
async def api_equity():
    return pos_store.get_equity()


@app.get("/api/mode")
async def api_mode():
    return {"mode": MODE}


@app.get("/api/debug/feed")
async def api_debug_feed():
    from deployment.live_feed import _raw_samples, _live_prices, _running, _debug_log
    return {
        "running":      _running,
        "prices_count": len(_live_prices),
        "prices":       dict(list(_live_prices.items())[:5]),
        "raw_samples":  _raw_samples,
        "log":          _debug_log,
    }


# ── DualMom endpoints ──────────────────────────────────────────────────────────

@app.get("/api/dualmom/stats")
async def api_dualmom_stats():
    return {
        **dualmom_engine.get_stats(),
        "last_updated": dualmom_engine.get_last_updated(),
    }


@app.get("/api/dualmom/signal")
async def api_dualmom_signal():
    return dualmom_engine.get_signal()


@app.get("/api/dualmom/portfolio")
async def api_dualmom_portfolio():
    live_prices = live_feed.get_live_prices() or {}
    return dualmom_engine.get_live_pnl(live_prices)


@app.get("/api/dualmom/equity")
async def api_dualmom_equity():
    return dualmom_engine.get_equity()


@app.get("/api/dualmom/paper")
async def api_dualmom_paper():
    return dualmom_paper.get_paper_state()


@app.get("/api/dualmom/paper_equity")
async def api_dualmom_paper_equity():
    return dualmom_paper.get_paper_equity()


@app.get("/api/dualmom/signal_log")
async def api_dualmom_signal_log():
    return dualmom_paper.get_signal_log()


@app.get("/api/dualmom/live_quotes")
async def api_dualmom_live_quotes():
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, dualmom_paper.get_live_quotes)


@app.post("/api/admin/dualmom_rebalance")
async def admin_dualmom_rebalance():
    """Manually trigger a month-end rebalance. Use when scheduler missed it."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, dualmom_paper.run_month_end_rebalance)
    return {"status": "ok", "message": "Rebalance triggered — check /api/dualmom/paper and signal_log for result."}


# ── Terminal (multi-broker positions) endpoint ───────────────────────────────

@app.get("/api/terminal")
async def api_terminal(force: bool = False):
    """Combined positions + P&L across Fyers, Zerodha, Jainam (read-only)."""
    from deployment.brokers import aggregator
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, aggregator.get_terminal, force)


@app.get("/api/terminal/eod_history")
async def api_terminal_eod_history():
    """Persisted daily XTS EOD P&L history (XTS resets daily)."""
    from deployment import broker_eod
    return broker_eod.get_history()


@app.get("/api/ticker")
async def api_ticker(force: bool = False):
    """Live ticker-tape quotes (Indian indices + large caps) for the Terminal tab."""
    from deployment import ticker
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, ticker.get_ticker, force)


# ── Strangle System endpoint ─────────────────────────────────────────────────

@app.get("/api/strangle/status")
async def api_strangle_status():
    """L1/L2/L3 signals + verdict + data-collection status for the strangle tab."""
    from strangle_system import dashboard_api as strangle_api
    # already off the loop; add a 5s cache so repeated polls don't rerun the ~3s compute
    return await asyncio.to_thread(
        lambda: _ttl_cached("strangle_status", 5.0, strangle_api.get_status))


# ── Strangle combined-premium chart archive (7-day rolling) ──────────────────
import json as _json

_CHART_DIR = (Path(__file__).parent.parent /
              "live_trading_options" / "strangle_strategy" / "data" / "chart_history")


@app.get("/api/strangle/charts")
async def api_strangle_charts(all: bool = False):
    """List archived combined-premium charts, newest first. By default only the
    DTE 0/1 trade days are returned (the days the strategy actually trades); the
    DTE>=2 chart-only days are hidden. Pass ?all=true to list every day."""
    if not _CHART_DIR.exists():
        return {"charts": []}
    out, seen = [], set()
    for f in sorted(_CHART_DIR.glob("*.json"), reverse=True):
        parts = f.stem.split("_")
        # V1 = {date}_{index} (2 parts); V2 = {date}_{index}_V2 (3 parts). List a day if
        # EITHER exists — so a day the tick engine captured (V2) shows even when the V1
        # capture scheduler didn't run.
        if len(parts) == 2:
            date, index = parts[0], parts[1]
        elif len(parts) == 3 and parts[2] == "V2":
            date, index = parts[0], parts[1]
        else:
            continue
        key = (date, index)
        if key in seen:
            continue
        seen.add(key)
        try:
            dte = (_json.loads(f.read_text()).get("selection") or {}).get("dte")
        except Exception:
            dte = None
        if all or dte in (0, 1):
            out.append({"date": date, "index": index, "dte": dte})
    return {"charts": out}


_LIVE_DIR = (Path(__file__).parent.parent /
             "live_trading_options" / "strangle_strategy" / "data" / "live_state")
_AUDIT_DIR = (Path(__file__).parent.parent /
              "live_trading_options" / "strangle_strategy" / "data" / "audit")


@app.get("/api/strangle/audit")
async def api_strangle_audit(date: str):
    """The permanent, append-only audit log for one trading day (both indices, real
    activity + system lifecycle). Read-only."""
    f = _AUDIT_DIR / f"{date}_audit.log"
    if not f.exists():
        return {"date": date, "text": ""}
    try:
        return {"date": date, "text": f.read_text(encoding="utf-8", errors="replace")}
    except Exception as e:
        return {"date": date, "text": "", "error": str(e)}


@app.get("/api/strangle/live_dates")
async def api_strangle_live_dates():
    """(date, index) pairs that have a live/paper-live state file, newest first."""
    if not _LIVE_DIR.exists():
        return {"days": []}
    out = []
    for f in sorted(_LIVE_DIR.glob("*_LIVE.json"), reverse=True):
        parts = f.stem.split("_")          # {date}_{index}_LIVE
        if len(parts) == 3:
            out.append({"date": parts[0], "index": parts[1]})
    return {"days": out}


def _live_control_mod():
    import sys as _s
    p = str(_LIVE_DIR.parent.parent)          # .../strangle_strategy
    if p not in _s.path:
        _s.path.append(p)
    from live import control_flags
    return control_flags


_MAX_LOTS = 15                                 # fat-finger cap (raise later as size grows)


def _strangle_lot_sizes() -> dict:
    """{INDEX: lot_size} from the strategy's parameters.json (authoritative — the SAME
    source the live controller sizes from). Falls back to the confirmed values."""
    try:
        pf = _LIVE_DIR.parent.parent / "config" / "parameters.json"
        return _json.loads(pf.read_text()).get("lot_sizes", {"NIFTY": 65, "SENSEX": 20})
    except Exception:
        return {"NIFTY": 65, "SENSEX": 20}


def _flag_index(index: str, broker: str) -> str:
    """Control-flag key for a broker+index. Kotak arms INDEPENDENTLY of Zerodha via a
    KOTAK_{INDEX} flag; Zerodha (default) uses the plain {INDEX}."""
    idx = index.upper()
    return f"KOTAK_{idx}" if str(broker).lower() == "kotak" else idx


def _snap_file(date: str, index: str, broker: str, kind: str):
    """State/tick file for a broker. kind='LIVE' or 'TICK'. Kotak files carry a KOTAK infix,
    so the two brokers never overwrite each other's snapshots."""
    idx = index.upper()
    if str(broker).lower() == "kotak":
        return _LIVE_DIR / (f"{date}_{idx}_KOTAK.json" if kind == "LIVE" else f"{date}_{idx}_KOTAK_TICK.json")
    return _LIVE_DIR / f"{date}_{idx}_{kind}.json"


@app.get("/api/strangle/live_control")
async def api_strangle_get_control(index: str = "NIFTY", broker: str = "zerodha"):
    """Current control flags (mode + kill + qty + mtm_stop) for one index+broker — NIFTY,
    SENSEX, and each broker (Zerodha vs Kotak) arm independently."""
    return _live_control_mod().read_control(_flag_index(index, broker))


@app.post("/api/strangle/live_control")
async def api_strangle_set_control(index: str = "NIFTY", broker: str = "zerodha",
                                   mode: str = None, kill: bool = None,
                                   qty: int = None, mtm_stop: float = None):
    """KILL switch / Paper-Live toggle + size/MTM override for ONE index+broker. Writes the
    flag file the matching controller reads (Zerodha: {INDEX}; Kotak: KOTAK_{INDEX}).

    qty (if given) must be a positive multiple of the index lot size, 1..15 lots — the
    server-side guard behind the browser validation. mtm_stop (if given) must be > 0.
    Rejected values return {"error": ...} and NOTHING is written."""
    idx = index.upper()
    if qty is not None:
        lot = _strangle_lot_sizes().get(idx)
        if not lot or qty <= 0 or qty % lot != 0 or (qty // lot) > _MAX_LOTS:
            return {"error": f"qty must be a positive multiple of {lot} "
                             f"(1–{_MAX_LOTS} lots) for {idx} — got {qty}"}
    if mtm_stop is not None and mtm_stop <= 0:
        return {"error": f"mtm_stop must be > 0 — got {mtm_stop}"}
    return _live_control_mod().write_control(index=_flag_index(index, broker), mode=mode,
                                             kill=kill, qty=qty, mtm_stop=mtm_stop)


@app.get("/api/strangle/live_tick")
async def api_strangle_live_tick(date: str, index: str, broker: str = "zerodha"):
    """Tiny real-time tick snapshot (combined premium, live MTM, per-leg LTP) the engine
    publishes every ~0.4s while running — polled fast by the Live tab. broker=kotak reads
    the Kotak mirror's tick. Read-only."""
    f = _snap_file(date, index, broker, "TICK")
    if not f.exists():
        return {"error": "no tick"}
    try:
        return _json.loads(f.read_text())
    except Exception:
        return {"error": "read"}


@app.get("/api/strangle/live")
async def api_strangle_live(date: str, index: str, broker: str = "zerodha"):
    """The live snapshot for one index/day/broker (cycles, order ids, P&L, status).
    broker=kotak returns the independent Kotak mirror's snapshot. Read-only."""
    f = _snap_file(date, index, broker, "LIVE")
    if not f.exists():
        return {"error": "not found", "date": date, "index": index, "broker": broker}
    return _json.loads(f.read_text())


@app.get("/api/strangle/chart")
async def api_strangle_chart(date: str, index: str, version: str = "V1"):
    """Return one archived combined-premium chart (candles + VWAP + signal events).
    version V1 = 1-minute reconstruction; V2 = live tick-built ({date}_{index}_V2.json)."""
    suffix = "_V2" if version.upper() == "V2" else ""
    f = _CHART_DIR / f"{date}_{index.upper()}{suffix}.json"
    if not f.exists():
        return {"error": "not found", "date": date, "index": index, "version": version.upper()}
    return _json.loads(f.read_text())


# ── Delta-Neutral Strangle endpoints ─────────────────────────────────────────
# Separate strategy, separate state directory, separate arm switch and audit log
# from the VWAP strangle above — the two must never be able to read or write each
# other's control flags. All read-only except /control.

_DN_DIR = (Path(__file__).parent.parent /
           "live_trading_options" / "delta_neutral" / "data" / "live_state")
_DN_ROOT = Path(__file__).parent.parent / "live_trading_options" / "delta_neutral"


def _dn_read(name: str) -> dict:
    f = _DN_DIR / name
    if not f.exists():
        return {"error": "not found", "file": name}
    try:
        return _json.loads(f.read_text())
    except Exception as e:
        return {"error": f"read failed: {e}"}


def _dn_params() -> dict:
    try:
        return _json.loads((_DN_ROOT / "config" / "parameters.json").read_text())
    except Exception:
        return {}


_dn_broker = None


def _dn_control_mod():
    """Load the delta-neutral control channel BY PATH, not by `import live.broker`.
    Both strategy packages have a `live/` subpackage and this process already has
    the VWAP strangle's on sys.path, so a plain import would resolve to whichever
    came first — i.e. it could arm the wrong strategy. Cached after first load."""
    global _dn_broker
    if _dn_broker is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_dn_broker", _DN_ROOT / "live" / "broker.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _dn_broker = mod
    return _dn_broker


@app.get("/api/dn/config")
async def api_dn_config():
    """Lot sizes, entry table and limits — so the UI never hardcodes them."""
    p = _dn_params()
    return {"lot_sizes": p.get("lot_sizes", {}), "entry": p.get("entry", {}),
            "max_lots": p.get("max_lots", 15), "max_loss": p.get("max_loss", 5000),
            "indices": p.get("live_orders", {}).get("indices", []),
            "ratio": p.get("adjust_trigger_ratio", 2.0),
            "square_off": p.get("square_off", "15:14"),
            "entry_time": p.get("entry_time", "09:30")}


def _book_suffix(book: str) -> str:
    """'' for the real book, '_PAPER' for the shadow benchmark that runs the same
    strategy on simulated fills. The gap between the two is execution cost."""
    return "_PAPER" if str(book).lower() == "paper" else ""


@app.get("/api/dn/status")
async def api_dn_status(date: str, index: str = "NIFTY", book: str = "live"):
    """Full snapshot for one index/day: legs, stops, adjustments, P&L, windows."""
    return _dn_read(f"{date}_{index.upper()}_DN{_book_suffix(book)}.json")


@app.get("/api/dn/tick")
async def api_dn_tick(date: str, index: str = "NIFTY", book: str = "live"):
    """Fast tick snapshot (~0.4s) — live premiums, MTM, next-window countdown."""
    return _dn_read(f"{date}_{index.upper()}_TICK{_book_suffix(book)}.json")


@app.get("/api/dn/compare")
async def api_dn_compare(date: str, index: str = "NIFTY"):
    """Live book vs paper benchmark, side by side.

    Same strategy, same ticks; the only difference is that one placed real orders
    and the other filled at the touch. So the delta is execution: slippage, fill
    quality, and anything the broker rejected."""
    idx = index.upper()
    out = {"date": date, "index": idx}
    for book, sfx in (("live", ""), ("paper", "_PAPER")):
        d = _dn_read(f"{date}_{idx}_DN{sfx}.json")
        if d.get("error"):
            out[book] = None
            continue
        pos = d.get("position") or {}
        out[book] = {
            "mtm": pos.get("mtm"), "realized": pos.get("realized"),
            "unrealized": pos.get("unrealized"),
            "closed_legs": len(pos.get("history") or []),
            "n_live": pos.get("n_live"), "shape": (
                "strangle" if pos.get("is_complete") else
                "single leg" if pos.get("is_single") else "flat"),
            "entries": d.get("fresh_entries"), "sl": d.get("sl"),
            "killed": d.get("killed"), "done": d.get("done"),
            "armed": d.get("armed"),
        }
    if out.get("live") and out.get("paper"):
        lm, pm = out["live"]["mtm"], out["paper"]["mtm"]
        if lm is not None and pm is not None:
            out["execution_cost"] = round(pm - lm, 2)   # paper minus real
    return out


@app.get("/api/dn/chain")
async def api_dn_chain(date: str, index: str = "NIFTY"):
    """The live option chain rows plus which strikes this strategy is short."""
    return _dn_read(f"{date}_{index.upper()}_CHAIN.json")


@app.get("/api/dn/dates")
async def api_dn_dates():
    """(date, index) pairs that have a snapshot, newest first."""
    if not _DN_DIR.exists():
        return {"days": []}
    out = []
    for f in sorted(_DN_DIR.glob("*_DN.json"), reverse=True):
        parts = f.stem.split("_")               # {date}_{index}_DN
        if len(parts) == 3:
            out.append({"date": parts[0], "index": parts[1]})
    return {"days": out}


@app.get("/api/dn/audit")
async def api_dn_audit(date: str):
    """This strategy's own append-only audit log for one day."""
    f = (_DN_ROOT.parent / "strangle_strategy" / "data" / "audit" / f"{date}_dn_audit.log")
    if not f.exists():
        return {"date": date, "text": ""}
    try:
        return {"date": date, "text": f.read_text(encoding="utf-8", errors="replace")}
    except Exception as e:
        return {"date": date, "text": "", "error": str(e)}


@app.get("/api/dn/control")
async def api_dn_get_control(index: str = "NIFTY"):
    return _dn_control_mod().read_control(index.upper())


@app.post("/api/dn/control")
async def api_dn_set_control(index: str = "NIFTY", mode: str = None, kill: bool = None,
                             qty: int = None, mtm_stop: float = None):
    """Arm / kill / resize for ONE index. Server-side validation behind the browser's:
    qty must be a positive multiple of that index's lot size within the lot cap, and
    the max loss must be positive. A rejected value writes NOTHING."""
    idx = index.upper()
    p = _dn_params()
    if qty is not None:
        lot = (p.get("lot_sizes") or {}).get(idx)
        cap = int(p.get("max_lots", 15))
        if not lot or qty <= 0 or qty % lot != 0 or (qty // lot) > cap:
            return {"error": f"qty must be a positive multiple of {lot} "
                             f"(1–{cap} lots) for {idx} — got {qty}"}
    if mtm_stop is not None and mtm_stop <= 0:
        return {"error": f"max loss must be > 0 — got {mtm_stop}"}
    return _dn_control_mod().write_control(idx, mode=mode, kill=kill,
                                           qty=qty, mtm_stop=mtm_stop)


# ── WebSocket — push updates every 60s during market hours, 5 min otherwise ──

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


async def _push_loop():
    """Background task: push signal + position snapshot to all connected browsers."""
    import json
    from datetime import datetime
    import pytz
    IST = pytz.timezone("Asia/Kolkata")

    while True:
        await asyncio.sleep(60)
        if not _ws_clients:
            continue
        try:
            today_prices = live_feed.get_live_prices() or None
            payload = {
                "type":      "update",
                "ts":        datetime.now(IST).strftime("%H:%M:%S"),
                "signals":   signal_engine.get_all_signals(today_prices),
                "positions": pos_store.get_positions(),
                "equity":    pos_store.get_equity(),
                "mode":      MODE,
                "feed":      live_feed.is_running(),
            }
            msg = json.dumps(payload)
            dead = []
            for ws in _ws_clients:
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _ws_clients.remove(ws)
        except Exception as e:
            print(f"  [push_loop] Error: {e}")


if __name__ == "__main__":
    uvicorn.run(
        "deployment.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
