"""
live_feed.py
Fyers WebSocket live price feed.
Subscribes to all 20 symbols, updates in-memory price dict every tick.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import os
import threading
from pathlib import Path

from fyers_apiv3 import fyersModel
from fyers_apiv3.FyersWebsocket import data_ws

from deployment.pair_config import PAIRS, SYM_A, SYM_B

ACCESS_TOKEN_PATH = Path(os.getenv(
    "ACCESS_TOKEN_PATH",
    r"G:\fyers_data_pipeline\config\access_token.txt"
))
APP_ID = os.getenv("FYERS_APP_ID", "W09OMXQB8J-100")

ALL_SYMS = list({p[SYM_A] for p in PAIRS} | {p[SYM_B] for p in PAIRS})
FYERS_SYMBOLS = [f"NSE:{sym}-EQ" for sym in ALL_SYMS]

_live_prices: dict[str, float] = {}
_lock          = threading.Lock()
_ws_client     = None
_running       = False
_extra_symbols: list[str] = []   # DualMom or other additional subscriptions
_raw_samples: list  = []          # DEBUG: store first 3 raw messages
_debug_log:   list  = []          # DEBUG: connection lifecycle events


def get_live_prices() -> dict[str, float]:
    with _lock:
        return dict(_live_prices)


def _on_message(msg):
    # DEBUG: capture first 3 raw messages to inspect format
    if len(_raw_samples) < 3:
        _raw_samples.append(msg)
        print(f"  [live_feed DEBUG] raw msg #{len(_raw_samples)}: {str(msg)[:300]}")

    # Fyers v3 WebSocket wraps ticks in {"d": [{symbol, ltp, ...}, ...]}
    # Some builds send the dict directly; handle both formats.
    ticks = []
    if isinstance(msg, dict):
        d = msg.get("d")
        if isinstance(d, list):
            ticks = d
        elif isinstance(d, dict):
            ticks = [d]
        else:
            ticks = [msg]          # flat format fallback
    elif isinstance(msg, list):
        ticks = msg

    for tick in ticks:
        if not isinstance(tick, dict):
            continue
        sym_raw = tick.get("symbol", "")
        ltp     = tick.get("ltp")
        if not sym_raw or ltp is None:
            continue
        sym = sym_raw.replace("NSE:", "").replace("-EQ", "")
        with _lock:
            _live_prices[sym] = float(ltp)


def _on_error(msg):
    print(f"  [live_feed] WebSocket error: {msg}")
    _debug_log.append({"event": "error", "msg": str(msg)[:200]})


def _on_close(msg):
    print(f"  [live_feed] WebSocket closed: {msg}")
    _debug_log.append({"event": "close", "msg": str(msg)[:200]})
    global _running
    _running = False


def _on_open():
    print("  [live_feed] WebSocket connected. Subscribing...")
    _debug_log.append({"event": "open", "syms": len(FYERS_SYMBOLS)})
    all_syms = FYERS_SYMBOLS + [f"NSE:{s}-EQ" for s in _extra_symbols if f"NSE:{s}-EQ" not in FYERS_SYMBOLS]
    _ws_client.subscribe(symbols=all_syms, data_type="SymbolUpdate")
    _debug_log.append({"event": "subscribed", "count": len(all_syms)})


def start_feed() -> bool:
    global _ws_client, _running

    token_path = ACCESS_TOKEN_PATH
    if not token_path.exists():
        print(f"  [live_feed] Token file not found: {token_path}")
        return False

    raw = token_path.read_text(encoding="utf-8").strip()
    if not raw:
        print("  [live_feed] Empty access token.")
        return False

    # token file is JSON: {"token": "...", "date": "..."}
    try:
        import json as _json
        payload      = _json.loads(raw)
        access_token = payload["token"]
        print(f"  [live_feed] Token parsed from JSON. date={payload.get('date')} len={len(access_token)} prefix={access_token[:12]}")
    except Exception as e:
        access_token = raw
        print(f"  [live_feed] Token read as plain string. len={len(raw)} (JSON parse error: {e})")

    client_id    = f"{APP_ID}:{access_token}"
    print(f"  [live_feed] Token path: {token_path}")

    _ws_client = data_ws.FyersDataSocket(
        access_token=client_id,
        log_path="",
        litemode=False,
        write_to_file=False,
        reconnect=True,
        on_connect=_on_open,
        on_close=_on_close,
        on_error=_on_error,
        on_message=_on_message,
    )

    def _run():
        global _running
        _running = True
        print("  [live_feed] Starting WebSocket thread...")
        _ws_client.connect()

    thread = threading.Thread(target=_run, daemon=True, name="FyersWS")
    thread.start()
    return True


EOD_SNAPSHOT_FILE = Path(__file__).parent / "eod_prices.json"


def save_eod_snapshot() -> None:
    """Persist last live prices as today's EOD close."""
    with _lock:
        prices = dict(_live_prices)
    if prices:
        from datetime import date
        data = {"date": date.today().isoformat(), "prices": prices}
        EOD_SNAPSHOT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"  [live_feed] EOD snapshot saved: {len(prices)} symbols")


def get_eod_snapshot() -> dict[str, float]:
    """Return today's EOD close prices (empty dict if no snapshot or stale date)."""
    if not EOD_SNAPSHOT_FILE.exists():
        return {}
    try:
        from datetime import date
        data = json.loads(EOD_SNAPSHOT_FILE.read_text(encoding="utf-8"))
        if data.get("date") == date.today().isoformat():
            return data.get("prices", {})
    except Exception:
        pass
    return {}


def stop_feed() -> None:
    global _running
    save_eod_snapshot()
    if _ws_client:
        try:
            _ws_client.close_connection()
        except Exception:
            pass
    _running = False


def is_running() -> bool:
    return _running


def add_symbols(symbols: list[str]) -> None:
    """Subscribe additional symbols (e.g. DualMom portfolio stocks) to live feed."""
    global _extra_symbols
    new_syms = [s for s in symbols if s not in ALL_SYMS and s not in _extra_symbols]
    if not new_syms:
        return
    _extra_symbols.extend(new_syms)
    if _running and _ws_client:
        fyers_syms = [f"NSE:{s}-EQ" for s in new_syms]
        try:
            _ws_client.subscribe(symbols=fyers_syms, data_type="SymbolUpdate")
            print(f"  [live_feed] Added {len(new_syms)} DualMom symbols to subscription.")
        except Exception as e:
            print(f"  [live_feed] add_symbols error: {e}")
