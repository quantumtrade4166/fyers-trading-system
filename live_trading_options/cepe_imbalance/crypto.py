"""
cepe_imbalance/crypto.py — BTC daily options from Delta Exchange India for the CE/PE PAPER books.
==================================================================================================

Owner 14-Nov-2026: run the CE/PE imbalance rules (normal + Supertrend variant) on BTC as PAPER, in two
versions on the same contracts (config.CRYPTO_SESSION):
  BTC      Indian day   09:00 -> square-off 17:20 IST, today's daily expiry (settles 17:30)
  BTCFULL  full expiry  17:30 -> square-off 17:20 the next day, the daily that lists at 17:30

Public data only — no API key, no account, nothing is ever sent to Delta except subscriptions:
  contracts  GET /v2/products (delta_btc.core.api.products), cached to data/delta_products_{date}.json
  ticks      wss://socket.india.delta.exchange
               mark_price  MARK:{symbol}  ~1 update/s: mark + best bid/ask + sizes   (measured 14-Nov)
               spot_price  .DEXBTUSD      several/s: the BTC index = reference price for ATM

Keys are "delta|{symbol}" (C-BTC-70000-141126) and "delta|.DEXBTUSD". Paper lot = C.DELTA_LOT contracts
(1000 = 1 BTC); contract value 0.001 BTC -> mult 0.001, so P&L is USD = premium x lots x 1000 x 0.001.
Quote.ltp = Delta's mark price (option last trades are often minutes old); fills still use bid / ask.
"""
import collections
import datetime as dt
import json
import logging
import threading
import time

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.instruments import IST, Contract

log = logging.getLogger("cepe.crypto")

WS_URL = "wss://socket.india.delta.exchange"
PREFIX = "delta|"
SPOT_KEY = PREFIX + C.DELTA_SPOT
STALE_S = 30                     # spot pushes several times a second: this much silence = dead socket


def names() -> list:
    return [n for n, s in C.UNDERLYINGS.items() if s["seg"] == "delta"]


# ── contracts ───────────────────────────────────────────────────────────────
def _cache_path(day: str):
    return C.DATA / f"delta_products_{day}.json"


def fetch_products() -> list:
    """Live BTC call/put products from Delta (public). Cached once a day."""
    from live_trading_options.delta_btc.core import api
    day = dt.datetime.now(IST).date().isoformat()
    rows = [r for r in api.products() if (r.get("underlying_asset") or {}).get("symbol") == "BTC"]
    keep = [{k: r.get(k) for k in ("id", "symbol", "contract_type", "strike_price", "settlement_time",
                                    "contract_value", "tick_size")} for r in rows]
    C.DATA.mkdir(parents=True, exist_ok=True)
    _cache_path(day).write_text(json.dumps(keep), encoding="utf-8")
    for old in C.DATA.glob("delta_products_*.json"):
        if old != _cache_path(day):
            old.unlink(missing_ok=True)
    return keep


def cached_products() -> list:
    """Newest products cache (the warm-up subprocess reads it instead of calling Delta)."""
    files = sorted(C.DATA.glob("delta_products_*.json"))
    return json.loads(files[-1].read_text(encoding="utf-8")) if files else []


def settle_date(iso: str) -> str | None:
    """'2026-11-14T12:00:00Z' -> IST date of the settlement ('2026-11-14')."""
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(IST).date().isoformat()


def contracts(products: list, keep_settled: bool = False) -> list:
    now = dt.datetime.now(IST)
    out = []
    for r in products:
        kind = {"call_options": "CE", "put_options": "PE"}.get(r.get("contract_type"))
        exp = settle_date(r.get("settlement_time"))
        if not kind or not exp:
            continue
        if not keep_settled:
            t = dt.datetime.fromisoformat(str(r["settlement_time"]).replace("Z", "+00:00"))
            if t <= now:
                continue
        try:
            strike, cv, tick = float(r["strike_price"]), float(r.get("contract_value") or 0.001), float(r.get("tick_size") or 0.1)
        except (TypeError, ValueError):
            continue
        out.append(Contract(key=PREFIX + r["symbol"], seg="delta", token=int(r.get("id") or 0), symbol=r["symbol"],
                            name="BTC", kind=kind, expiry=exp, strike=strike, lot=C.DELTA_LOT, tick=tick, mult=cv))
    return out


def attach(universe, products: list, keep_settled: bool = False):
    """Fill every Delta underlying (BTC, BTCFULL) of `universe` with the same contracts."""
    cs = contracts(products, keep_settled=keep_settled)
    for n in names():
        u = universe.underlyings.get(n)
        if u is None:
            continue
        u.settle = C.CRYPTO_SETTLE
        u.chains = {}
        for c in cs:
            universe.by_key[c.key] = c
            u.chains.setdefault(c.expiry, {}).setdefault(c.strike, {})[c.kind] = c
        for e in list(u.chains):
            u.chains[e] = {k: v for k, v in sorted(u.chains[e].items()) if "CE" in v and "PE" in v}
            if not u.chains[e]:
                del u.chains[e]
        u.lot, u.mult = C.DELTA_LOT, (cs[0].mult if cs else 0.001)
    if cs:
        log.info("BTC: %d option contracts, expiries %s", len(cs), sorted({c.expiry for c in cs})[:4])
    return universe


def load_into(universe):
    """Engine start: products from Delta; a Delta outage must never stop the Kotak instruments."""
    try:
        prods = fetch_products()
    except Exception as e:
        log.warning("Delta products unavailable (%s); using the last cache", e)
        prods = cached_products()
    return attach(universe, prods)


# ── feed ────────────────────────────────────────────────────────────────────
def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _i(v):
    x = _f(v)
    return int(x) if x is not None else None


def decode(m: dict, recv_ts: float) -> Tick | None:
    t = m.get("type")
    if t == "spot_price" and m.get("symbol") == C.DELTA_SPOT:
        p = _f(m.get("price"))
        return Tick(SPOT_KEY, p, None, None, None, None, None, None, int(recv_ts), recv_ts) if p else None
    if t == "mark_price":
        sym = str(m.get("symbol") or "")
        sym = sym[5:] if sym.startswith("MARK:") else sym
        p = _f(m.get("price"))
        if not sym or p is None:
            return None
        bid, ask = _f(m.get("best_bid")), _f(m.get("best_ask"))
        return Tick(PREFIX + sym, p, bid or None, ask or None, _i(m.get("bid_qty")), _i(m.get("ask_qty")),
                    None, None, int(recv_ts), recv_ts)
    return None


class DeltaFeed:
    """Public Delta socket: spot + marks of every wanted option. Same shape as feed.MultiFeed."""

    def __init__(self, on_ticks):
        self.on_ticks = on_ticks
        self.wanted: list = []
        self._wanted_set: set = set()
        self.sent: set = set()
        self.ws = None
        self.connected = False
        self.ticks = 0
        self.last_tick_at = 0.0
        self.connects = 0
        self.last_error = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._q = collections.deque()
        self._has = threading.Event()
        self.batches = 0
        self.backlog_max = 0.0

    def start(self):
        threading.Thread(target=self._work, daemon=True, name="DeltaTicks").start()
        threading.Thread(target=self._run, daemon=True, name="DeltaFeed").start()

    def stop(self):
        self._stop.set()
        try:
            self.ws and self.ws.close()
        except Exception:
            pass

    def want(self, keys) -> int:
        with self._lock:
            new = [k for k in dict.fromkeys(keys) if k.startswith(PREFIX) and k not in self._wanted_set]
            for k in new:
                self._wanted_set.add(k)
                self.wanted.append(k)
        if new and self.connected:
            self._send(new)
        return len(new)

    def status(self) -> dict:
        return {"connected": self.connected, "subscribed": len(self.sent), "wanted": len(self.wanted),
                "ticks": self.ticks, "last_tick_at": self.last_tick_at, "connects": self.connects,
                "error": self.last_error, "queued": len(self._q), "batches": self.batches,
                "backlog_max_s": round(self.backlog_max, 2)}

    def _send(self, keys=None):
        """(Re)subscribe EVERYTHING wanted in one message — a later subscribe for the same channel may
        replace the earlier symbol list, so never send partial lists."""
        with self._lock:
            syms = [k[len(PREFIX):] for k in self.wanted if k != SPOT_KEY]
        ch = [{"name": "spot_price", "symbols": [C.DELTA_SPOT]}]
        if syms:
            ch.append({"name": "mark_price", "symbols": ["MARK:" + s for s in syms]})
        try:
            self.ws.send(json.dumps({"type": "subscribe", "payload": {"channels": ch}}))
            with self._lock:
                self.sent = {PREFIX + s for s in syms} | {SPOT_KEY}
        except Exception as e:
            self.last_error = f"subscribe: {e}"
            log.warning("Delta subscribe failed: %s", e)

    def _run(self):
        import websocket
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self.ws = websocket.create_connection(WS_URL, timeout=20)
                self.connected = True
                self.connects += 1
                self.sent = set()
                self._send()
                log.info("Delta feed connected; %d keys", len(self.wanted))
                t0 = time.time()
                self.ws.settimeout(STALE_S)
                while not self._stop.is_set():
                    raw = self.ws.recv()                     # raises on close / timeout -> reconnect
                    if not raw:
                        raise ConnectionError("socket closed")
                    self._q.append((time.time(), raw))
                    self._has.set()
                if time.time() - t0 > 60:
                    backoff = 2.0
            except Exception as e:
                if not self._stop.is_set():
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    log.warning("Delta feed: %s (reconnect in %.0f s)", self.last_error, backoff)
            finally:
                self.connected = False
                try:
                    self.ws and self.ws.close()
                except Exception:
                    pass
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 60.0)

    def _work(self):
        while not self._stop.is_set():
            self._has.wait(1.0)
            self._has.clear()
            while self._q:
                batch, lag = [], 0.0
                while self._q:
                    ts, raw = self._q.popleft()
                    lag = max(lag, time.time() - ts)
                    try:
                        t = decode(json.loads(raw), ts)
                    except ValueError:
                        t = None
                    if t:
                        batch.append(t)
                self.backlog_max = max(self.backlog_max, lag)
                self.batches += 1
                if not batch:
                    continue
                self.ticks += len(batch)
                self.last_tick_at = time.time()
                try:
                    self.on_ticks(batch)
                except Exception:
                    log.exception("Delta on_ticks failed")


class CompositeFeed:
    """Kotak feed + Delta feed behind the one `engine.feed` the engine knows (want / status / start / stop)."""

    def __init__(self, kotak, delta):
        self.kotak, self.delta = kotak, delta

    def want(self, keys) -> int:
        keys = list(keys)
        d = [k for k in keys if k.startswith(PREFIX)]
        k = [x for x in keys if not x.startswith(PREFIX)]
        return (self.kotak.want(k) if k else 0) + (self.delta.want(d) if d else 0)

    def status(self) -> dict:
        return {**self.kotak.status(), "delta": self.delta.status()}

    def start(self):
        self.kotak.start()
        self.delta.start()

    def stop(self):
        self.kotak.stop()
        self.delta.stop()
