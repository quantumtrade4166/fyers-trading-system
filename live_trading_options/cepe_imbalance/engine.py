"""
cepe_imbalance/engine.py — routes Kotak ticks to one paper book + strategy per underlying.
=========================================================================================

Threads:
  feed thread   ticks -> Market -> strategy.on_tick for every RUNNING underlying the ticks touched
  timer thread  every second: re-centre chain subscriptions, on_timer, session-end square-off,
                MTM/imbalance history sample, day rollover
One engine lock serialises every strategy call, so rules never run twice at once.
A strategy exception is logged and counted; it never stops the feed or other underlyings.
"""
import datetime as dt
import json
import logging
import threading
import time

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import strategy as S
from live_trading_options.cepe_imbalance.book import IST, PaperBook, today

log = logging.getLogger("cepe.engine")


# ── session hours ───────────────────────────────────────────────────────────
def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def us_dst(day: dt.date) -> bool:
    """US daylight saving: 2nd Sunday of March -> 1st Sunday of November."""
    return _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)


def session(name: str, day: dt.date) -> tuple:
    kind = C.UNDERLYINGS[name]["kind"]
    if kind == "index":
        o, c = C.NSE_HOURS
    else:
        o, c = C.MCX_HOURS_US_DST if us_dst(day) else C.MCX_HOURS_US_STD
    t = lambda s: dt.datetime.combine(day, dt.time.fromisoformat(s), IST)
    close = t(c)
    return t(o), close, close - dt.timedelta(minutes=C.SQUARE_OFF_BEFORE_MIN[kind])


# ── per-underlying context given to the strategy ────────────────────────────
class Context:
    def __init__(self, runner):
        self._r = runner
        self.name = runner.name
        self.book = runner.book
        self.market = runner.market

    @property
    def params(self):
        return self._r.params

    @property
    def state(self):
        return self._r.state

    @property
    def now(self):
        return dt.datetime.now(IST)

    @property
    def expiry(self):
        return self.market.expiry(self.name, self._r.expiry_n)

    def ref_price(self):
        return self.market.ref_price(self.name, self.expiry)

    def synthetic(self):
        """Options-implied price (strike + CE - PE). Use for NIFTY/SENSEX after 15:15 (closing auction)."""
        return self.market.synthetic(self.name, self.expiry)

    def atm(self):
        return self.market.atm(self.name, self.expiry)

    def contract(self, strike, kind, expiry=None):
        return self.market.contract(self.name, expiry or self.expiry, strike, kind)

    def quote(self, key):
        return self.market.quote(key)

    def window(self, each_side=C.STRIKES_EACH_SIDE):
        return self.market.window(self.name, self.expiry, each_side)

    def log(self, event, **fields):
        self._r.event(event, **fields)


class Runner:
    def __init__(self, name, market, strategy_name=S.DEFAULT, date=None, persist=True):
        self.name = name
        self.market = market
        self.persist = persist
        self.date = date or today()
        self.book = PaperBook(name, market, self.date, persist=persist)
        self.strategy_name = strategy_name
        self.strategy = S.REGISTRY[strategy_name]()
        self.params = dict(getattr(self.strategy, "params", {}) or {})
        self.expiry_n = 0
        self.running = False
        self.square_off_done = False
        self.state: dict = {}
        self.errors = 0
        self.last_error = ""
        self.events: list = []
        self.ctx = Context(self)
        if persist:
            self._load_state()

    # ── persistence ─────────────────────────────────────────────────────────
    def _state_path(self):
        d = C.DATA / "books" / self.date
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.name}_state.json"

    def _load_state(self):
        p = self._state_path()
        if p.exists():
            doc = json.loads(p.read_text(encoding="utf-8"))
            self.running = bool(doc.get("running"))
            self.square_off_done = bool(doc.get("square_off_done"))
            self.state = doc.get("state") or {}
            self.params.update(doc.get("params") or {})
            self.expiry_n = int(doc.get("expiry_n", 0))

    def save_state(self):
        if not self.persist:
            return
        p = self._state_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"running": self.running, "square_off_done": self.square_off_done,
                                   "strategy": self.strategy_name, "params": self.params,
                                   "expiry_n": self.expiry_n, "state": self.state}, indent=1, default=str),
                       encoding="utf-8")
        tmp.replace(p)

    def event(self, event, **fields):
        e = {"ts": time.time(), "time": dt.datetime.now(IST).strftime("%H:%M:%S"), "event": event, **fields}
        self.events.append(e)
        del self.events[:-500]
        if self.persist:
            with open(C.DATA / "books" / self.date / f"{self.name}_events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(e, default=str) + "\n")

    # ── strategy calls (engine holds its lock) ──────────────────────────────
    def call(self, fn, *a):
        try:
            fn(self.ctx, *a)
        except Exception as e:
            self.errors += 1
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            log.exception("%s strategy error", self.name)
            self.event("strategy_error", error=self.last_error)


class Engine:
    def __init__(self, universe, market, feed=None, persist=True):
        self.u = universe
        self.market = market
        self.feed = feed
        self.persist = persist
        self.lock = threading.RLock()
        self.runners = {n: Runner(n, market, persist=persist) for n in C.ORDER if n in universe.underlyings}
        self.routes: dict[str, set] = {}          # feed key -> {underlying names}
        self._stop = threading.Event()
        self._last_sample = 0.0
        self._last_subs = 0.0
        self.started_at = time.time()

    # ── subscriptions ───────────────────────────────────────────────────────
    def wanted_keys(self, name) -> list:
        und = self.u.underlyings[name]
        keys = [und.spot_key] if und.spot_key else []
        keys += [f.key for f in und.futures[:3]]
        for n in range(C.EXPIRIES_LIVE):
            e = self.market.expiry(name, n)
            if not e:
                continue
            if self.market.ref_price(name, e) <= 0:
                continue                           # ATM unknown yet: subscribe the reference first
            for s in self.market.window(name, e):
                for kind in ("CE", "PE"):
                    c = self.market.contract(name, e, s, kind)
                    if c:
                        keys.append(c.key)
        r = self.runners.get(name)                 # open positions are always watched
        if r:
            keys += [k for k, p in r.book.positions.items() if p.lots]
        return keys

    def refresh_subscriptions(self) -> int:
        new = 0
        for name in self.runners:
            keys = self.wanted_keys(name)
            for k in keys:
                self.routes.setdefault(k, set()).add(name)
            if self.feed:
                new += self.feed.want(keys)
        return new

    # ── feed callback ───────────────────────────────────────────────────────
    def on_ticks(self, ticks):
        keys = self.market.apply(ticks)
        touched: dict[str, list] = {}
        for k in keys:
            for n in self.routes.get(k, ()):
                touched.setdefault(n, []).append(k)
        if not touched:
            return
        with self.lock:
            for n, ks in touched.items():
                r = self.runners[n]
                if r.running and not r.square_off_done:
                    r.call(r.strategy.on_tick, ks)

    # ── controls ────────────────────────────────────────────────────────────
    def start(self, name):
        with self.lock:
            r = self.runners[name]
            if r.running:
                return {"ok": True, "note": "already running"}
            r.running, r.square_off_done = True, False
            r.event("start", strategy=r.strategy_name)
            r.call(r.strategy.on_start)
            r.save_state()
            return {"ok": True}

    def stop(self, name, exit_positions=False):
        with self.lock:
            r = self.runners[name]
            r.running = False
            out = r.book.exit_all("stop") if exit_positions else []
            r.event("stop", exited=len(out))
            r.save_state()
            return {"ok": True, "exits": out}

    def manual_order(self, name, strike, kind, side, lots, expiry_n=None):
        with self.lock:
            r = self.runners[name]
            e = self.market.expiry(name, r.expiry_n if expiry_n is None else int(expiry_n))
            c = self.market.contract(name, e, float(strike), kind.upper())
            if not c:
                return {"ok": False, "error": f"no contract {name} {e} {strike} {kind}"}
            if self.feed:
                self.feed.want([c.key])
            self.routes.setdefault(c.key, set()).add(name)
            t = r.book.order(c, side, int(lots), reason="manual")
            r.event("manual_order", **{k: t.get(k) for k in ("symbol", "side", "lots", "price", "ok", "error")})
            return t

    # ── timer ───────────────────────────────────────────────────────────────
    def tick_timer(self, now: dt.datetime | None = None):
        now = now or dt.datetime.now(IST)
        if now.date().isoformat() != next(iter(self.runners.values())).date:
            self.rollover(now.date().isoformat())
        if time.time() - self._last_subs >= 5:
            self._last_subs = time.time()
            self.refresh_subscriptions()
        with self.lock:
            for n, r in self.runners.items():
                if not r.running or r.square_off_done:
                    continue
                o, close, sq = session(n, now.date())
                if now >= sq:
                    r.square_off_done = True
                    r.event("session_end_square_off")
                    r.call(r.strategy.on_session_end)
                    r.save_state()
                elif now >= o:
                    r.call(r.strategy.on_timer)
        if time.time() - self._last_sample >= C.HISTORY_EVERY_S:
            self._last_sample = time.time()
            self.sample(now)

    def sample(self, now):
        if not self.persist:
            return
        d = C.DATA / "history" / now.date().isoformat()
        d.mkdir(parents=True, exist_ok=True)
        for n, r in self.runners.items():
            has_book = any(p.lots or p.realized for p in r.book.positions.values())
            if not (r.running or has_book):
                continue
            m, im = r.book.mtm(), r.book.imbalance()
            row = {"t": round(time.time(), 1), "mtm": m["total"], "imb": im["pct"],
                   "ref": self.market.ref_price(n), "atm": self.market.atm(n)}
            with open(d / f"{n}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")

    def rollover(self, date):
        """New day: fresh paper books (no carry), runners stopped."""
        with self.lock:
            for n, r in list(self.runners.items()):
                if any(p.lots for p in r.book.positions.values()):
                    r.event("rollover_with_open_positions_dropped")
                self.runners[n] = Runner(n, self.market, r.strategy_name, date=date, persist=self.persist)
        log.info("day rollover -> %s", date)

    def run_timer(self):
        def loop():
            while not self._stop.wait(1.0):
                try:
                    self.tick_timer()
                except Exception:
                    log.exception("timer")
        threading.Thread(target=loop, daemon=True, name="CepeTimer").start()

    def stop_all(self):
        self._stop.set()

    # ── read model for the API ──────────────────────────────────────────────
    def summary(self) -> dict:
        now = dt.datetime.now(IST)
        out = []
        for n, r in self.runners.items():
            o, close, sq = session(n, now.date())
            e = self.market.expiry(n, r.expiry_n)
            rk = self.market.ref_key(n, e)
            rq = self.market.quote(rk) if rk else None
            out.append({"name": n, "label": C.UNDERLYINGS[n]["label"], "running": r.running,
                        "strategy": r.strategy_name, "expiry": e, "lot": self.u.underlyings[n].lot,
                        "ref": rq.ltp if rq else None, "ref_age": round(time.time() - rq.recv_ts, 1) if rq else None,
                        "atm": self.market.atm(n, e), "synthetic": self.market.synthetic(n, e), "mtm": r.book.mtm(), "imbalance": r.book.imbalance(),
                        "open_positions": sum(1 for p in r.book.positions.values() if p.lots),
                        "trades": len(r.book.trades), "errors": r.errors, "last_error": r.last_error,
                        "session": {"open": o.strftime("%H:%M"), "close": close.strftime("%H:%M"),
                                    "square_off": sq.strftime("%H:%M"), "in_session": o <= now < close},
                        "square_off_done": r.square_off_done})
        return {"now": now.isoformat(timespec="seconds"), "underlyings": out,
                "feed": self.feed.status() if self.feed else None, "universe_date": self.u.file_date}
