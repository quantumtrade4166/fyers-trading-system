"""
engine.py — the delta-neutral strangle live engine.
===================================================

STANDALONE process with its OWN Fyers WebSocket. Streams the option chain around
ATM for each configured index, keeps a `LiveChain` current from the ticks, and
drives one `DNController` per index.

Deliberately a separate process from the VWAP strangle's `live_tick_engine.py`
and from `deployment/live_feed.py`: each strategy owning its own socket means a
fault in one can never disturb another that is holding real positions. It also
holds its own single-instance lock (port 47653).

Run (system python — fyers_apiv3 lives there, not in .venv):

    python live_trading_options/delta_neutral/engine.py

Start it any time from ~09:20; it seeds from the current chain, and a mid-day
restart recovers the day's real position from the broker before trading again.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import time
import threading
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fyers_apiv3.FyersWebsocket import data_ws

from core.shared import (symbol_master, fyers_client, singleton, is_trade_day,
                         PORT_DN_ENGINE)
from core.chain import LiveChain
from live.controller import DNController
from live.broker import audit_log

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MKT_END = dt.time(15, 30)

# how often the controllers are stepped. Ticks arrive far faster than any decision
# the strategy makes (the tightest is a 60-second window), so stepping on every
# tick would just burn CPU re-deciding the same thing.
_STEP_SECONDS = 0.2

_chains: dict[str, LiveChain] = {}
_ctrls: dict[str, DNController] = {}
_sym_index: dict[str, str] = {}        # fyers symbol -> index it belongs to
_ws = None
_last_tick = None
_lock = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


# ── setup ─────────────────────────────────────────────────────────────────
def build(date_str: str):
    """One chain + one controller per configured index."""
    kite = _get_kite()
    for index in PARAMS["live_orders"].get("indices", []):
        try:
            tradeable, dte, expiry = is_trade_day(index)
        except Exception as e:
            print(f"  [dn] {index} expiry lookup failed: {e}")
            continue

        chain = LiveChain(index, expiry, PARAMS["strike_interval"][index],
                          PARAMS["index_symbols"][index], span=PARAMS.get("chain_span", 15))
        chain.load_strikes()

        # Lot size: prefer the BROKER's own figure for the exact contract — it is
        # what the order will actually be sized in, and it changes from time to
        # time (NIFTY has been 50, 75 and 65). parameters.json is the fallback.
        lot = PARAMS["lot_sizes"].get(index, 1)
        if kite:
            try:
                from live.broker import kite_executor as kx
                probe = chain.band(chain.atm or chain._all_strikes[len(chain._all_strikes) // 2])
                c = kx.resolve(kite, index, expiry, probe[len(probe) // 2], "CE")
                if c.get("lot_size"):
                    if int(c["lot_size"]) != lot:
                        print(f"  [dn] {index}: lot size {lot} (config) -> "
                              f"{c['lot_size']} (broker) — using the broker's")
                    lot = int(c["lot_size"])
            except Exception as e:
                print(f"  [dn] {index}: broker lot size unavailable, using config {lot} ({e})")

        ctrl = DNController(index, date_str, expiry, dte, params=PARAMS,
                            lot_size=lot, kite=kite,
                            symbol_lookup=chain.symbol_for)
        # A mid-day (re)start must recover the real position BEFORE the first tick
        # reaches the strategy — otherwise the next adjustment window sees "flat"
        # and opens a second strangle on top of the live one.
        try:
            ctrl.reconcile_broker()
        except Exception as e:
            print(f"  [dn] {index} reconcile failed: {e}")

        _chains[index], _ctrls[index] = chain, ctrl
        print(f"  [dn] {index}: expiry={expiry} dte={dte} "
              f"{'TRADES' if tradeable else 'no-trade day (chart only)'} "
              f"target={ctrl.target} sl={ctrl.sl} lot={lot}")
        audit_log(index, "ENGINE_BUILD", expiry=str(expiry), dte=dte,
                  tradeable=tradeable, target=ctrl.target, sl=ctrl.sl,
                  broker_ready=bool(kite))


def _get_kite():
    """Kite client for real orders, or None (paper-only) if it can't be built."""
    try:
        from live.broker import kite_executor as kx
        k = kx.get_kite()
        k.profile()                              # prove the token actually works
        return k
    except Exception as e:
        print(f"  [dn] Kite unavailable — paper only: {e}")
        return None


def _subscribe(symbols: list[str], index: str):
    if not symbols:
        return
    for s in symbols:
        _sym_index[s] = index
    try:
        _ws.subscribe(symbols=symbols, data_type="SymbolUpdate")
        _chains[index].mark_subscribed(symbols)
        print(f"  [dn] {index}: subscribed {len(symbols)} contracts")
    except Exception as e:
        print(f"  [dn] {index} subscribe failed: {e}")


def _seed_spot():
    """One REST quote per index so ATM is known before the first tick — otherwise
    we would not know which strikes to subscribe to."""
    try:
        client = fyers_client.get_client()
    except Exception as e:
        print(f"  [dn] no Fyers client for spot seed: {e}")
        return
    syms = [c.index_symbol for c in _chains.values()]
    try:
        resp = client.quotes({"symbols": ",".join(syms)})
        for row in (resp.get("d") or []):
            n, v = row.get("n"), row.get("v") or {}
            for c in _chains.values():
                if c.index_symbol == n and v.get("lp") is not None:
                    c.on_tick(n, float(v["lp"]))
                    print(f"  [dn] {c.index}: spot {c.spot} atm {c.atm}")
    except Exception as e:
        print(f"  [dn] spot seed failed: {e}")


# ── websocket ─────────────────────────────────────────────────────────────
def _on_message(msg):
    """A single bad tick must never escape and kill the socket's read loop."""
    global _last_tick
    try:
        ticks = []
        if isinstance(msg, dict):
            d = msg.get("d")
            ticks = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [msg])
        elif isinstance(msg, list):
            ticks = msg
        for t in ticks:
            try:
                if not isinstance(t, dict):
                    continue
                sym, ltp = t.get("symbol"), t.get("ltp")
                if not sym or ltp is None:
                    continue
                index = _sym_index.get(sym)
                if not index:
                    continue
                with _lock:
                    _chains[index].on_tick(sym, float(ltp))
                _last_tick = time.monotonic()
            except Exception as e:
                print(f"  [dn] tick skipped: {e}")
    except Exception as e:
        print(f"  [dn] on_message error: {e}")


def _on_open():
    for index, chain in _chains.items():
        _subscribe(chain.symbols_to_add(), index)


def _on_error(m): print(f"  [dn] WS error: {m}")
def _on_close(m): print(f"  [dn] WS closed: {m}")


# ── the strategy loop ─────────────────────────────────────────────────────
def _step_loop(date_str: str):
    """Step every controller on a fixed cadence. Runs in its own thread so a slow
    order round-trip inside a controller can never block the socket's reader."""
    while True:
        time.sleep(_STEP_SECONDS)
        now = _now()
        if now.time() > MKT_END:
            return
        for index, ctrl in list(_ctrls.items()):
            chain = _chains[index]
            try:
                if not chain.is_ready():
                    continue
                with _lock:
                    snap, spot = chain.chain(), chain.spot
                ctrl.on_tick(snap, spot, now)
            except Exception as e:
                # never let one index's failure stop the other's — it may be holding
                # a real position that still needs its square-off
                print(f"  [dn] {index} step error: {e}", flush=True)
                audit_log(index, "STEP_ERROR", error=f"{type(e).__name__}: {e}")


def _writer_loop(date_str: str, every: int = 10):
    """Persist snapshots, extend the subscribed band as spot drifts, self-heal a
    silent socket stall, and exit cleanly after the close."""
    STALL_SECS = 90
    while True:
        time.sleep(every)
        now = _now()
        for index, ctrl in list(_ctrls.items()):
            try:
                ctrl.persist()
                _write_chain(index, date_str)
            except Exception as e:
                print(f"  [dn] {index} persist error: {e}")
            try:                                  # follow spot if it has drifted
                with _lock:
                    add = _chains[index].symbols_to_add()
                _subscribe(add, index)
            except Exception:
                pass

        if now.time() > dt.time(15, 35):
            print("  [dn] market closed — final snapshots written, exiting.")
            audit_log("SYSTEM", "ENGINE_EXIT", reason="market closed")
            # hard exit: the Fyers WS keeps the main thread blocked forever, so a
            # plain return would leave a zombie that blocks tomorrow's start
            os._exit(0)

        if (_last_tick is not None and (time.monotonic() - _last_tick) > STALL_SECS
                and dt.time(9, 20) < now.time() < dt.time(15, 30)):
            print(f"  [dn] tick stall >{STALL_SECS}s — exiting for a clean restart")
            audit_log("SYSTEM", "ENGINE_STALL", seconds=int(time.monotonic() - _last_tick))
            os._exit(1)


def _write_chain(index: str, date_str: str):
    """Publish the chain table the terminal renders."""
    from live.controller import STATE_DIR
    with _lock:
        chain = _chains[index]
        rows, spot, atm, upd = chain.rows(), chain.spot, chain.atm, chain.updated
    ctrl = _ctrls[index]
    sold = {}
    for leg in (ctrl.position.ce, ctrl.position.pe):
        if leg is not None and leg.is_live:
            sold[f"{leg.strike}{leg.opt_type}"] = {
                "entry": leg.entry_price, "sl": leg.sl_trigger,
                "status": leg.status, "qty": leg.qty}
    (STATE_DIR / f"{date_str}_{index}_CHAIN.json").write_text(json.dumps({
        "index": index, "spot": spot, "atm": atm, "rows": rows, "sold": sold,
        "expiry": str(chain.expiry), "updated": upd,
        "written": dt.datetime.now().strftime("%H:%M:%S")}))


# ── main ──────────────────────────────────────────────────────────────────
def main():
    global _ws
    if not singleton.acquire(PORT_DN_ENGINE):
        print("  [dn] another engine holds the lock — this duplicate exits.")
        return

    st = fyers_client.token_status()
    print(f"  [dn] Fyers token date={st['date']} valid={st['valid']}")
    audit_log("SYSTEM", "ENGINE_START", pid=os.getpid(), fyers_valid=st.get("valid"))
    if not st["valid"]:
        print("  [dn] Fyers token invalid — aborting.")
        return

    date_str = dt.date.today().isoformat()
    build(date_str)
    if not _ctrls:
        print("  [dn] no indices configured — aborting.")
        return
    _seed_spot()

    _ws = data_ws.FyersDataSocket(
        access_token=f"{fyers_client.CLIENT_ID}:{fyers_client.load_raw_token()}",
        log_path="", litemode=False, write_to_file=False, reconnect=True,
        on_connect=_on_open, on_close=_on_close, on_error=_on_error,
        on_message=_on_message)

    threading.Thread(target=_step_loop, args=(date_str,), daemon=True, name="dn-step").start()
    threading.Thread(target=_writer_loop, args=(date_str,), daemon=True, name="dn-writer").start()
    _ws.connect()


if __name__ == "__main__":
    main()
