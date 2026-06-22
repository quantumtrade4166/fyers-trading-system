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
from deployment import signal_engine, positions as pos_store, live_feed, dualmom_engine

MODE = os.getenv("TRADING_MODE", "paper").upper()

_scheduler      = None
_ws_clients: list[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
    print(f"\n  ── Pairs Dashboard starting ({MODE} mode) ──")
    signal_engine.init_engine()
    dualmom_engine.refresh()

    _scheduler = create_scheduler()
    _scheduler.start()
    print("  [main] Scheduler started.")

    # if server starts during market hours, kick off the live feed immediately
    import pytz
    from datetime import datetime
    _ist = pytz.timezone("Asia/Kolkata")
    _now = datetime.now(_ist)
    _market_open = _now.weekday() < 5 and (9, 15) <= (_now.hour, _now.minute) <= (15, 30)
    if _market_open:
        print("  [main] Market is open — starting live feed immediately.")
        live_feed.start_feed()

    asyncio.create_task(_push_loop())
    yield

    if _scheduler:
        _scheduler.shutdown(wait=False)
    live_feed.stop_feed()
    print("  [main] Shutdown complete.")


app = FastAPI(title="Pairs Dashboard", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── REST endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def api_status():
    return {
        "mode":        MODE,
        "feed_active": live_feed.is_running(),
        "live_prices": len(live_feed.get_live_prices()),
    }


@app.get("/api/signals")
async def api_signals():
    today_prices = live_feed.get_live_prices() or None
    return signal_engine.get_all_signals(today_prices)


@app.get("/api/positions")
async def api_positions():
    positions = pos_store.get_positions()
    live_prices = live_feed.get_live_prices() or {}
    signals = signal_engine.get_all_signals(live_prices or None)

    for name, pos in positions.items():
        sig = signals.get(name, {})
        cur_a = live_prices.get(pos.get("sym_a", ""), sig.get("price_a", pos["entry_price_a"]))
        cur_b = live_prices.get(pos.get("sym_b", ""), sig.get("price_b", pos["entry_price_b"]))
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
        now = datetime.now(IST)
        market_open = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
        await asyncio.sleep(15 if market_open else 60)
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
