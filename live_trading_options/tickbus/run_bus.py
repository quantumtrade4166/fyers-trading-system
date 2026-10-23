"""
tickbus/run_bus.py — the LIVE tick-bus process (Stage 1+).
==========================================================

Owns the single real KiteTicker and serves it to local consumers. Self-contained:
reads KITE_API_KEY from deployment/.env and today's token from deployment/zerodha_token.json
(the same shared Kite session the engines already use) — it does NOT depend on any strategy
package, because the bus is platform infrastructure that sits UNDER the strategies.

Run by a Windows task (boot + watchdog restart). Exits non-zero on a fatal error so the
relauncher restarts it; consumers ride the gap via BusTicker auto-reconnect.

Start manually:  .venv\\Scripts\\python.exe live_trading_options\\tickbus\\run_bus.py
"""

import os
import sys
import json
import datetime as dt
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]                 # .../fyers_data_pipeline
sys.path.insert(0, str(REPO / "live_trading_options"))     # so `import tickbus.*` resolves

from tickbus.bus_server import TickBus                       # noqa: E402


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / "deployment" / ".env")
    except Exception:
        pass


def _kite_ticker():
    """Build a real KiteTicker on the shared Kite session (today's token only)."""
    from kiteconnect import KiteTicker
    api_key = os.getenv("KITE_API_KEY", "")
    if not api_key:
        raise RuntimeError("KITE_API_KEY missing in deployment/.env")
    tok_file = REPO / "deployment" / "zerodha_token.json"
    d = json.loads(tok_file.read_text(encoding="utf-8"))
    if d.get("date") != dt.date.today().isoformat():
        raise RuntimeError(f"Kite token is for {d.get('date')}, not today — run zerodha_login first")
    return KiteTicker(api_key, d.get("access_token", ""))


def main():
    _load_env()
    hb = REPO / "live_trading_options" / "tickbus" / "data" / "bus_heartbeat.txt"
    hb.parent.mkdir(parents=True, exist_ok=True)
    ticker = _kite_ticker()
    bus = TickBus(ticker, heartbeat_path=str(hb))
    print(f"[tickbus] starting live bus (heartbeat {hb})", flush=True)
    bus.start(threaded=False)     # blocks until stopped / process killed


if __name__ == "__main__":
    main()
