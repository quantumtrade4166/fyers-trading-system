"""
engine.py — the delta-neutral strangle live engine.
===================================================

STANDALONE process with its own **Kite** WebSocket. Streams the option chain
around ATM for each configured index, keeps a `LiveChain` current from the ticks,
and drives one `DNController` per index.

Kite for data as well as orders, deliberately:
  - the VWAP strangle owns the Fyers socket. Two Fyers connections on one token
    risk the broker dropping one, and whichever strategy loses its feed goes
    blind — no stop monitoring, no square-off. Different provider, no contention.
  - prices arrive from the venue the orders go to, so a stop trigger is measured
    against the book it will execute against.
  - no Fyers symbol master and no pandas in the runtime path.

It also holds its own single-instance lock (port 47653), separate from the
dashboard's and the VWAP engine's.

Run (VPS venv python — that is where pandas/kiteconnect live):

    .venv\\Scripts\\python.exe live_trading_options/delta_neutral/engine.py

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

from core.shared import singleton, PORT_DN_ENGINE
from core.chain import LiveChain, is_trade_day
from live.controller import DNController, STATE_DIR
from live.broker import audit_log, kite_executor as kx

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MKT_END = dt.time(15, 30)

# Ticks arrive far faster than any decision this strategy makes (the tightest is
# a 60-second window), so stepping on every tick would burn CPU re-deciding the
# same thing.
_STEP_SECONDS = 0.2

_chains: dict[str, LiveChain] = {}
_ctrls: dict[str, DNController] = {}          # real book
_shadows: dict[str, DNController] = {}        # paper benchmark
_token_index: dict[int, str] = {}      # instrument_token -> index it belongs to
_kws = None
_last_tick = None
_lock = threading.Lock()


def _now() -> dt.datetime:
    return dt.datetime.now(IST).replace(tzinfo=None)


# ── setup ─────────────────────────────────────────────────────────────────
def build(date_str: str, kite):
    """One chain + one controller per configured index."""
    for index in PARAMS["live_orders"].get("indices", []):
        try:
            tradeable, dte, expiry = is_trade_day(kite, index)
        except Exception as e:
            print(f"  [dn] {index} expiry lookup failed: {e}")
            continue

        chain = LiveChain(index, expiry, PARAMS["strike_interval"][index], kite,
                          span=PARAMS.get("chain_span", 15))
        try:
            chain.load()
        except Exception as e:
            print(f"  [dn] {index} chain load failed: {e}")
            continue

        # Kite's own lot size for this contract wins over config — it is what the
        # order is actually sized in, and it has changed before (50 -> 75 -> 65).
        lot = chain.lot_size or PARAMS["lot_sizes"].get(index, 1)
        if lot != PARAMS["lot_sizes"].get(index):
            print(f"  [dn] {index}: lot size {PARAMS['lot_sizes'].get(index)} (config) "
                  f"-> {lot} (Kite) — using Kite's")

        ctrl = DNController(index, date_str, expiry, dte, params=PARAMS,
                            lot_size=lot, kite=kite, symbol_lookup=chain.symbol_for)
        # The paper benchmark: same strategy, same ticks, simulated fills. Runs
        # regardless of arming, so the difference between the two books at the end
        # of the day IS the cost of execution.
        shadow = DNController(index, date_str, expiry, dte, params=PARAMS,
                              lot_size=lot, kite=None,
                              symbol_lookup=chain.symbol_for, shadow=True)
        # A mid-day (re)start must recover the real position BEFORE the first tick
        # reaches the strategy — otherwise the next window sees "flat" and opens a
        # second strangle on top of the live one.
        try:
            ctrl.reconcile_broker()
        except Exception as e:
            print(f"  [dn] {index} reconcile failed: {e}")

        _chains[index], _ctrls[index] = chain, ctrl
        _shadows[index] = shadow
        print(f"  [dn] {index}: expiry={expiry} dte={dte} "
              f"{'TRADES' if tradeable else 'no-trade day (chain only)'} "
              f"target={ctrl.target} sl={ctrl.sl} lot={lot} "
              f"strikes={len(chain.strikes)} spot_token={chain.spot_token}")
        audit_log(index, "ENGINE_BUILD", expiry=str(expiry), dte=dte,
                  tradeable=tradeable, target=ctrl.target, sl=ctrl.sl, lot=lot,
                  feed="kite")


def _seed_spot(kite):
    """One REST quote per index so ATM is known before the first tick — otherwise
    we would not know which strikes to subscribe to."""
    want = {i: f"{c.index}" for i, c in _chains.items()}
    del want
    syms = []
    from core.chain import SPOT
    for index in _chains:
        exch, name = SPOT[index]
        syms.append(f"{exch}:{name}")
    try:
        q = kite.quote(syms)
    except Exception as e:
        print(f"  [dn] spot seed failed: {e}")
        return
    for index, chain in _chains.items():
        exch, name = SPOT[index]
        row = q.get(f"{exch}:{name}") or {}
        lp = row.get("last_price")
        if lp:
            chain.on_tick(chain.spot_token, float(lp))
            print(f"  [dn] {index}: spot {chain.spot} atm {chain.atm}")


def _subscribe(tokens: list[int], index: str):
    if not tokens:
        return
    for t in tokens:
        _token_index[int(t)] = index
    try:
        _kws.subscribe(tokens)
        _kws.set_mode(_kws.MODE_LTP, tokens)      # last price is all we need
        _chains[index].mark_subscribed(tokens)
        print(f"  [dn] {index}: subscribed {len(tokens)} instruments")
    except Exception as e:
        print(f"  [dn] {index} subscribe failed: {e}")


# ── websocket ─────────────────────────────────────────────────────────────
def _on_ticks(ws, ticks):
    """A single bad tick must never escape and kill the socket's read loop."""
    global _last_tick
    try:
        for t in ticks:
            try:
                tok = t.get("instrument_token")
                ltp = t.get("last_price")
                if tok is None or ltp is None:
                    continue
                index = _token_index.get(int(tok))
                if not index:
                    continue
                with _lock:
                    _chains[index].on_tick(tok, float(ltp))
                _last_tick = time.monotonic()
            except Exception as e:
                print(f"  [dn] tick skipped: {e}")
    except Exception as e:
        print(f"  [dn] on_ticks error: {e}")


def _on_connect(ws, response):
    print("  [dn] Kite ticker connected")
    for index, chain in _chains.items():
        _subscribe(chain.tokens_to_add(), index)


def _on_close(ws, code, reason):
    print(f"  [dn] ticker closed: {code} {reason}")


def _on_error(ws, code, reason):
    print(f"  [dn] ticker error: {code} {reason}")


# ── the strategy loop ─────────────────────────────────────────────────────
def _step_loop():
    """Step every controller on a fixed cadence. Its own thread, so a slow order
    round-trip inside a controller can never block the socket's reader."""
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
                sh = _shadows.get(index)
                if sh is not None:
                    sh.on_tick(snap, spot, now)
            except Exception as e:
                # never let one index's failure stop the other's — it may be
                # holding a real position that still needs its square-off
                print(f"  [dn] {index} step error: {e}", flush=True)
                audit_log(index, "STEP_ERROR", error=f"{type(e).__name__}: {e}")


def _writer_loop(date_str: str, every: int = 10):
    """Persist snapshots, extend the subscription as spot drifts, self-heal a
    silent stall, and exit cleanly after the close."""
    STALL_SECS = 90
    while True:
        time.sleep(every)
        now = _now()
        for index, ctrl in list(_ctrls.items()):
            try:
                ctrl.persist()
                sh = _shadows.get(index)
                if sh is not None:
                    sh.persist()
                _write_chain(index, date_str)
            except Exception as e:
                print(f"  [dn] {index} persist error: {e}")
            try:
                with _lock:
                    add = _chains[index].tokens_to_add()
                _subscribe(add, index)
            except Exception:
                pass

        if now.time() > dt.time(15, 35):
            print("  [dn] market closed — final snapshots written, exiting.")
            audit_log("SYSTEM", "ENGINE_EXIT", reason="market closed")
            os._exit(0)

        if (_last_tick is not None and (time.monotonic() - _last_tick) > STALL_SECS
                and dt.time(9, 20) < now.time() < dt.time(15, 30)):
            print(f"  [dn] tick stall >{STALL_SECS}s — exiting for a clean restart")
            audit_log("SYSTEM", "ENGINE_STALL", seconds=int(time.monotonic() - _last_tick))
            os._exit(1)


def _write_chain(index: str, date_str: str):
    """Publish the chain table the terminal renders."""
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
        "expiry": str(chain.expiry), "updated": upd, "feed": "kite",
        "written": dt.datetime.now().strftime("%H:%M:%S")}))


# ── main ──────────────────────────────────────────────────────────────────
def main():
    global _kws
    if not singleton.acquire(PORT_DN_ENGINE):
        print("  [dn] another engine holds the lock — this duplicate exits.")
        return

    try:
        kite = kx.get_kite()
        who = kite.profile().get("user_id")
        print(f"  [dn] Kite auth ok: {who}")
    except Exception as e:
        print(f"  [dn] Kite unavailable — aborting: {e}")
        audit_log("SYSTEM", "ENGINE_ABORT", reason=f"kite auth: {e}")
        return

    audit_log("SYSTEM", "ENGINE_START", pid=os.getpid(), feed="kite", user=who)
    date_str = dt.date.today().isoformat()
    build(date_str, kite)
    if not _ctrls:
        print("  [dn] no indices configured — aborting.")
        return
    _seed_spot(kite)

    from kiteconnect import KiteTicker
    _kws = KiteTicker(kx._API_KEY, kx.access_token())
    _kws.on_ticks = _on_ticks
    _kws.on_connect = _on_connect
    _kws.on_close = _on_close
    _kws.on_error = _on_error

    threading.Thread(target=_step_loop, daemon=True, name="dn-step").start()
    threading.Thread(target=_writer_loop, args=(date_str,), daemon=True,
                     name="dn-writer").start()
    _kws.connect()          # blocks, reconnects on its own


if __name__ == "__main__":
    main()
