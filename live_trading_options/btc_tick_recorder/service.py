"""
btc_tick_recorder/service.py — EVERY tick of the whole Delta Exchange BTC option chain, 24/7 (owner 15-Nov-2026).
===============================================================================================================

"I want every tick of the option chain recorded, properly." One public Delta websocket (no key, no
account), every live BTC option of EVERY expiry (~630 contracts; new dailies list at 17:30 IST and are
added within PRODUCTS_EVERY_S), plus the BTC index:

  l1_orderbook  best bid / ask + sizes, on every change        -> a QUOTE row      (~500 /s)
  mark_price    Delta's mark price, ~1 /s per contract         -> a QUOTE row      (~300 /s)
  v2/ticker     open interest + 24h volume, ~every 5 s         -> carried on the next quote row (no row)
  all_trades    every trade                                    -> a TRADE row
  spot_price    .DEXBTUSD (the index options settle on)        -> a QUOTE row

A quote row is the contract's FULL state at that instant: ltp = mark, bid, ask, bid size, ask size,
volume (24h contracts), oi (contracts), exch_ts (exchange seconds); receive time to the millisecond.
A trade row: ltp = price, bid_qty = size if the BUYER was the taker, ask_qty = size if the SELLER was,
volume = size, exch_ts = trade time (s).

Files (same proven format as the NIFTY/SENSEX recorder, tick_recorder/store.py):
  data/ticks_btc/{IST date}/BTC_{HHMM}.tcol      quotes, 15-min slots   (journal *.trec.tmp until packed)
  data/ticks_btc/{IST date}/BTCTRD_{HHMM}.tcol   trades
  data/ticks_btc/{IST date}/BTC_gaps.jsonl       every disconnect: from / to / reason (so a gap is never silent)
Prices are cents (int), exact for Delta's $0.1 ticks; mark is rounded to the cent.

Read: tick_recorder.store.read_day_arrays(date, "BTC", Path("data/ticks_btc")) / read_day(...)

Process: scheduled task BtcTickRecorder (boot + every 5 min, run_btc_ticks.bat with crash back-off);
port 127.0.0.1:47696 = single-instance lock + GET /health.
"""
import collections
import datetime as dt
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.tick_recorder.store import TickStore

log = logging.getLogger("btcticks")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "ticks_btc"               # own root: the NIFTY recorder packs EVERY journal under data/ticks
LOG_DIR = ROOT / "logs"
HOST, PORT = "127.0.0.1", 47696
WS_URL = "wss://socket.india.delta.exchange"
SPOT = ".DEXBTUSD"
PREFIX = "delta|"
QUOTES, TRADES = "BTC", "BTCTRD"
PRODUCTS_EVERY_S = 300
STALE_S = 20                      # spot alone pushes several times a second: this much silence = dead socket


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return int(x) if x is not None and x >= 0 else None


def _sec(us):
    """Delta timestamps are microseconds."""
    x = _f(us)
    return int(x / 1e6) if x else 0


def _sym(s):
    s = str(s or "")
    return s[5:] if s.startswith("MARK:") else s


class State:
    """Latest full quote state per symbol; turns Delta messages into store rows."""

    def __init__(self):
        self.q: dict[str, dict] = {}

    def _st(self, sym):
        s = self.q.get(sym)
        if s is None:
            s = self.q[sym] = {"mark": None, "bid": None, "ask": None, "bq": None, "aq": None,
                               "vol": None, "oi": None}
        return s

    def _row(self, sym, s, ets, recv):
        return Tick(PREFIX + sym, s["mark"] or 0.0, s["bid"], s["ask"], s["bq"], s["aq"], s["vol"], s["oi"],
                    ets, recv)

    def on(self, m: dict, recv: float):
        """-> (index, Tick) or None."""
        t = m.get("type")
        if t == "l1_orderbook":
            sym = _sym(m.get("symbol"))
            if not sym:
                return None
            s = self._st(sym)
            b, a = _f(m.get("best_bid")), _f(m.get("best_ask"))
            s["bid"], s["ask"] = (b if b and b > 0 else None), (a if a and a > 0 else None)
            s["bq"] = _i(m.get("bid_qty")) if s["bid"] else None
            s["aq"] = _i(m.get("ask_qty")) if s["ask"] else None
            return QUOTES, self._row(sym, s, _sec(m.get("last_updated_at") or m.get("timestamp")), recv)
        if t == "mark_price":
            sym = _sym(m.get("symbol"))
            p = _f(m.get("price"))
            if not sym or p is None:
                return None
            s = self._st(sym)
            s["mark"] = p
            return QUOTES, self._row(sym, s, _sec(m.get("timestamp")), recv)
        if t == "v2/ticker":
            sym = _sym(m.get("symbol"))
            if sym:
                s = self._st(sym)
                oi, vol = _i(m.get("oi_contracts")), _i(m.get("size"))
                s["oi"] = oi if oi is not None else s["oi"]
                s["vol"] = vol if vol is not None else s["vol"]
            return None
        if t == "all_trades":
            sym = _sym(m.get("symbol"))
            p, size = _f(m.get("price")), _i(m.get("size"))
            if not sym or p is None or not size:
                return None
            buy_taker = m.get("buyer_role") == "taker"
            return TRADES, Tick(PREFIX + sym, p, None, None, size if buy_taker else None,
                                None if buy_taker else size, size, None, _sec(m.get("timestamp")), recv)
        if t == "spot_price" and m.get("symbol") == SPOT:
            p = _f(m.get("price"))
            return (QUOTES, Tick(PREFIX + SPOT, p, None, None, None, None, None, None, int(recv), recv)) if p else None
        return None


def contract_info_from(products: dict):
    def info(key):
        sym = key.split("|", 1)[-1]
        c = products.get(sym)
        if c:
            return sym, c["kind"], c["expiry"], c["strike"]
        return sym, "INDEX", "", 0
    return info


def fetch_products() -> dict:
    """symbol -> {kind, expiry (IST date of settlement), strike} for every live BTC option."""
    from live_trading_options.delta_btc.core import api
    out = {}
    for r in api.products():
        if (r.get("underlying_asset") or {}).get("symbol") != "BTC":
            continue
        kind = {"call_options": "CE", "put_options": "PE"}.get(r.get("contract_type"))
        if not kind:
            continue
        try:
            exp = dt.datetime.fromisoformat(str(r["settlement_time"]).replace("Z", "+00:00")).astimezone(IST).date()
            out[r["symbol"]] = {"kind": kind, "expiry": exp.isoformat(), "strike": float(r["strike_price"])}
        except (KeyError, TypeError, ValueError):
            continue
    return out


class Recorder:
    def __init__(self, store_root: Path = DATA, start_store=True):
        self.products: dict = {}
        self.store = TickStore(store_root, contract_info_from(self.products), start=start_store)
        self.root = Path(store_root)
        self.state = State()
        self.subscribed: set = set()
        self.ws = None
        self.connected = False
        self.connects = 0
        self.last_msg = 0.0
        self.counts = collections.Counter()
        self.errors = 0
        self.last_error = ""
        self.down_since: float | None = time.time()
        self.down_reason = "start"
        self.gaps = 0
        self._q = collections.deque()
        self._has = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ── products / subscriptions ────────────────────────────────────────────
    def refresh_products(self) -> int:
        try:
            p = fetch_products()
        except Exception as e:
            self.last_error = f"products: {e}"[:200]
            log.warning("products refresh failed: %s", e)
            return 0
        with self._lock:
            new = [s for s in p if s not in self.products]
            self.products.update(p)                         # expired symbols stay known (old files read fine)
        if new:
            log.info("products: +%d (total live %d)", len(new), len(p))
            self.subscribe(new)
        return len(new)

    def payload(self, syms) -> str:
        syms = sorted(syms)
        ch = [{"name": "spot_price", "symbols": [SPOT]}]
        if syms:
            ch += [{"name": "l1_orderbook", "symbols": syms},
                   {"name": "mark_price", "symbols": ["MARK:" + s for s in syms]},
                   {"name": "v2/ticker", "symbols": syms},
                   {"name": "all_trades", "symbols": syms}]
        return json.dumps({"type": "subscribe", "payload": {"channels": ch}})

    def subscribe(self, syms):
        """Delta ADDS to an existing subscription (tested 15-Nov), so only new symbols are sent."""
        ws = self.ws
        if not (ws and self.connected):
            return                                           # the next connect subscribes everything
        try:
            ws.send(self.payload(syms))
            self.subscribed.update(syms)
        except Exception as e:
            self.last_error = f"subscribe: {e}"[:200]

    # ── gaps ────────────────────────────────────────────────────────────────
    def gap_closed(self, now):
        if self.down_since is None:
            return
        rec = {"from": dt.datetime.fromtimestamp(self.down_since, IST).isoformat(timespec="milliseconds"),
               "to": dt.datetime.fromtimestamp(now, IST).isoformat(timespec="milliseconds"),
               "seconds": round(now - self.down_since, 3), "reason": self.down_reason}
        p = self.root / dt.datetime.fromtimestamp(now, IST).date().isoformat() / "BTC_gaps.jsonl"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            log.exception("gap log")
        self.gaps += 1
        log.info("feed gap %.1f s (%s)", rec["seconds"], rec["reason"])
        self.down_since = None

    # ── socket thread: only receive + queue (the Kotak lesson of 13-Nov) ────
    def run_socket(self):
        import websocket
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(WS_URL, timeout=20)
                self.ws = ws
                self.connected = True
                self.connects += 1
                with self._lock:
                    syms = list(self.products)
                ws.send(self.payload(syms))
                self.subscribed = set(syms)
                ws.settimeout(STALE_S)
                first = True
                t0 = time.time()
                while not self._stop.is_set():
                    raw = ws.recv()
                    if not raw:
                        raise ConnectionError("socket closed")
                    now = time.time()
                    if first:
                        self.gap_closed(now)
                        first = False
                    self.last_msg = now
                    self._q.append((now, raw))
                    self._has.set()
                if time.time() - t0 > 60:
                    backoff = 1.0
            except Exception as e:
                if not self._stop.is_set():
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("socket: %s (reconnect in %.0f s)", self.last_error, backoff)
                    if self.down_since is None:
                        self.down_since, self.down_reason = time.time(), self.last_error
            finally:
                self.connected = False
                try:
                    self.ws and self.ws.close()
                except Exception:
                    pass
                self.ws = None
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def run_worker(self):
        while not self._stop.is_set():
            self._has.wait(1.0)
            self._has.clear()
            while self._q:
                recv, raw = self._q.popleft()
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                self.counts[m.get("type") or "?"] += 1
                try:
                    out = self.state.on(m, recv)
                except Exception as e:
                    self.errors += 1
                    self.last_error = f"decode {type(e).__name__}: {e}"[:200]
                    continue
                if out:
                    self.store.add(*out)

    def start(self):
        threading.Thread(target=self.run_worker, daemon=True, name="BtcTicksWork").start()
        threading.Thread(target=self.run_socket, daemon=True, name="BtcTicksSocket").start()

    def stop(self):
        self._stop.set()
        try:
            self.ws and self.ws.close()
        except Exception:
            pass
        self.store.stop()

    def health(self) -> dict:
        age = time.time() - self.last_msg if self.last_msg else None
        return {"ok": bool(self.connected and age is not None and age < STALE_S), "connected": self.connected,
                "last_msg_age_s": round(age, 2) if age is not None else None, "contracts_live": len(self.products),
                "subscribed": len(self.subscribed), "connects": self.connects, "gaps_logged": self.gaps,
                "queued": len(self._q), "messages": dict(self.counts), "errors": self.errors,
                "last_error": self.last_error, "store": dict(self.store.stats)}


def serve_health(rec, sock):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(rec.health()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    sock.listen(16)
    srv = ThreadingHTTPServer((HOST, PORT), H, bind_and_activate=False)
    srv.socket.close()
    srv.socket = sock
    threading.Thread(target=srv.serve_forever, daemon=True, name="BtcTicksHealth").start()


def main():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(LOG_DIR / "btc_tick_recorder.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])
    sock = socket.socket()
    try:
        sock.bind((HOST, PORT))                       # single instance
    except OSError:
        return 0                                      # already running
    rec = Recorder()
    rec.refresh_products()
    if not rec.products:
        log.error("no BTC products from Delta; exiting for a retry")
        return 2
    serve_health(rec, sock)
    rec.start()
    log.info("BTC tick recorder up: %d contracts", len(rec.products))
    last_products = time.time()
    try:
        while True:
            time.sleep(60)
            if time.time() - last_products >= PRODUCTS_EVERY_S:
                last_products = time.time()
                rec.refresh_products()
            h = rec.health()
            st = h["store"]
            m = h["messages"]
            log.info("contracts %d  connected %s  msgs l1 %s mark %s trades %s  rows %d  journal MB %.1f  "
                     "kept MB %.1f  packed %d  dropped %d  pack errors %d  gaps %d  queued %d",
                     h["contracts_live"], h["connected"], m.get("l1_orderbook", 0), m.get("mark_price", 0),
                     m.get("all_trades", 0), st["rows"], st["journal_bytes"] / 1e6, st["tcol_bytes"] / 1e6,
                     st["packed"], st["dropped_frames"], st["pack_errors"], h["gaps_logged"], h["queued"])
    finally:
        rec.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
