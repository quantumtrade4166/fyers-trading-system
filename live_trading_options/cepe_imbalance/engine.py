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
from live_trading_options.cepe_imbalance import st_signal as STS
from live_trading_options.cepe_imbalance import strategy as S
from live_trading_options.cepe_imbalance.book import IST, PaperBook, atomic_write, today
from live_trading_options.cepe_imbalance.quotes import QuoteRecorder

log = logging.getLogger("cepe.engine")


# ── durable per-instrument settings (survive day rollover + restarts) ───────
# data/settings.json = {name: {"params": {...}, "auto_start": bool, "auto_time": "HH:MM"}}
_settings_lock = threading.Lock()


def settings_path():
    return C.DATA / "settings.json"


def load_settings() -> dict:
    p = settings_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        log.exception("settings.json unreadable; using defaults")
        return {}


def save_settings(name, **fields):
    with _settings_lock:
        doc = load_settings()
        doc.setdefault(name, {}).update(fields)
        p = settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, json.dumps(doc, indent=1))


def default_auto(name) -> dict:
    kind = C.UNDERLYINGS[C.und_of(name)]["kind"]
    times = C.ST_AUTO_START_TIME if C.is_variant(name) else C.AUTO_START_TIME
    return {"auto_start": C.AUTO_START_DEFAULT, "auto_time": times[kind]}


# ── session hours ───────────────────────────────────────────────────────────
def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def us_dst(day: dt.date) -> bool:
    """US daylight saving: 2nd Sunday of March -> 1st Sunday of November."""
    return _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)


def session(name: str, day: dt.date) -> tuple:
    kind = C.UNDERLYINGS[C.und_of(name)]["kind"]
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
        self.name = runner.und              # the UNDERLYING (market data, strike step); the book is runner.name
        self.book = runner.book
        self.market = runner.market
        self.signal = None                  # Supertrend variant: fn(period, mult) -> st_signal state dict

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

    def save(self):
        self._r.save_state()

    def in_session(self, now=None):
        """Open <= now < square-off time."""
        now = now or self.now
        o, _close, sq = session(self.name, now.date())
        return o <= now < sq


class Runner:
    def __init__(self, name, market, strategy_name=None, date=None, persist=True):
        self.name = name                    # book name: "NIFTY" or the Supertrend variant "NIFTY_ST"
        self.und = C.und_of(name)
        strategy_name = strategy_name or ("cepe_st" if C.is_variant(name) else S.DEFAULT)
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
        self.auto = default_auto(name)
        if persist:
            saved = load_settings().get(name, {})
            self.params.update({k: v for k, v in (saved.get("params") or {}).items() if k in self.params})
            self.auto.update({k: saved[k] for k in ("auto_start", "auto_time") if k in saved})
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
        atomic_write(p, json.dumps({"running": self.running, "square_off_done": self.square_off_done,
                                    "strategy": self.strategy_name, "params": self.params,
                                    "expiry_n": self.expiry_n, "state": self.state}, indent=1, default=str))

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
        # books: one normal + one Supertrend variant ("NIFTY_ST") per underlying
        self.runners = {}
        for n in C.ALL_BOOKS:
            if C.und_of(n) in universe.underlyings:
                self.runners[n] = self._attach(Runner(n, market, persist=persist))
        self.unds = [n for n in C.ORDER if n in universe.underlyings]
        self.signals = {u: STS.StSignal(u) for u in self.unds}
        self.routes: dict[str, set] = {}          # feed key -> {book names}
        self._stop = threading.Event()
        self._last_sample = 0.0
        self._last_st = 0.0
        self._last_subs = 0.0
        self.started_at = time.time()
        self.hist: dict[str, list] = {n: [] for n in self.runners}   # today's MTM/imbalance samples
        self.quotes = QuoteRecorder(universe) if persist else None    # every tick, for exact replays
        if persist:
            self._load_hist(today())

    def _attach(self, r):
        """Give a Supertrend variant book its signal (the underlying's shared StSignal)."""
        if C.is_variant(r.name):
            r.ctx.signal = lambda per, mul, u=r.und: self.signals[u].state(per, mul)
        return r

    # ── Supertrend signal (variant books) ───────────────────────────────────
    def st_params(self, und):
        r = self.runners.get(und + C.VARIANT_SUFFIX)
        p = r.params if r else {}
        step = float(p.get("step") or C.STRIKE_STEP.get(und) or 0)
        return step, float(p.get("atm_buffer_pct", 20.0)), (r.expiry_n if r else 0)

    def st_sample(self, now: dt.datetime):
        t = time.time()
        for u in self.unds:
            step, buf, en = self.st_params(u)
            e = self.market.expiry(u, en)
            sig = self.signals[u]
            day = now.date().isoformat()
            if sig.day != day or sig.expiry != e:            # new day / expiry: fresh candles + warm-up
                sig.reset(day, e)
                if e:
                    self.start_warmup(u, day, e, step, buf)
            o, close, _sq = session(u, now.date())
            if e:
                sig.sample(t, self.market, step, buf, o.timestamp(), close.timestamp())

    def start_warmup(self, und, day, expiry, step, buf):
        """Queue a warm-up; ONE worker runs them one after another (each is a 10-40 s replay process)."""
        if not hasattr(self, "_warm_q"):
            import queue
            self._warm_q = queue.Queue()

            def worker():
                while True:
                    fn = self._warm_q.get()
                    try:
                        fn()
                    except Exception:
                        log.exception("warm-up worker")
            threading.Thread(target=worker, daemon=True, name="StWarmup").start()
        sig = self.signals[und]

        def job():
            try:
                if self.persist:
                    time.sleep(C.QUOTES_FLUSH_WAIT_S)          # let the quote recorder write the last ticks
                    w, tb, src, wd = STS.warm_up_subprocess(und, day, expiry, step, buf)
                else:
                    w, tb, src, wd = [], [], "none", None
                if sig.day == day and sig.expiry == expiry:
                    sig.install_warmup(w, tb, src, wd)
                    log.info("%s Supertrend warm-up: %d bars from %s, %d bars of today replayed",
                             und, len(w), src, len(tb))
            except Exception:
                log.exception("%s Supertrend warm-up failed (today only)", und)
                if sig.day == day and sig.expiry == expiry:
                    sig.install_warmup([], [], "none (error)", None)
        if self.persist:
            self._warm_q.put(job)
        else:
            job()                                            # tests / demo: no files, instant

    def _load_hist(self, date):
        for n in self.runners:
            p = C.DATA / "history" / date / f"{n}.jsonl"
            rows = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
            self.hist[n] = [json.loads(x) for x in rows if x.strip()]

    # ── subscriptions ───────────────────────────────────────────────────────
    def wanted_keys(self, name) -> list:
        """name = book name; market data is the underlying's (NIFTY_ST watches NIFTY)."""
        u = C.und_of(name)
        und = self.u.underlyings[u]
        keys = [und.spot_key] if und.spot_key else []
        keys += [f.key for f in und.futures[:3]]
        for n in range(C.EXPIRIES_LIVE):
            e = self.market.expiry(u, n)
            if not e:
                continue
            if self.market.ref_price(u, e) <= 0:
                continue                           # ATM unknown yet: subscribe the reference first
            for s in self.market.window(u, e):
                for kind in ("CE", "PE"):
                    c = self.market.contract(u, e, s, kind)
                    if c:
                        keys.append(c.key)
        r = self.runners.get(name)
        e = self.market.expiry(u, r.expiry_n if r else 0)          # strategy grid around ATM
        atm = self.market.atm(u, e) if e else None
        if atm is not None:
            step = float((r.params.get("step") if r else 0) or C.STRIKE_STEP.get(u) or 0)
            if step > 0:
                base = round(atm / step) * step
                for i in range(-C.GRID_EACH_SIDE, C.GRID_EACH_SIDE + 1):
                    for kind in ("CE", "PE"):
                        c = self.market.contract(u, e, base + i * step, kind)
                        if c:
                            keys.append(c.key)
        # open positions are always watched
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
        rec = self.quotes
        for k in keys:
            names = self.routes.get(k, ())
            for n in names:
                touched.setdefault(n, []).append(k)
            if rec and names:                                   # once per UNDERLYING, not per book
                q = self.market.quote(k)
                if q:
                    for u in {C.und_of(n) for n in names}:
                        rec.add(u, k, q, q.recv_ts or None)
        if not touched:
            return
        with self.lock:
            for n, ks in touched.items():
                r = self.runners[n]
                if r.running and not r.square_off_done:
                    r.call(r.strategy.on_tick, ks)

    # ── controls ────────────────────────────────────────────────────────────
    def start(self, name, how="manual"):
        with self.lock:
            r = self.runners[name]
            r.state["auto_done"] = True                     # any start/stop today ends auto-start for today
            if r.running:
                return {"ok": True, "note": "already running"}
            r.running, r.square_off_done = True, False
            r.event("start", strategy=r.strategy_name, how=how)
            r.call(r.strategy.on_start)
            r.save_state()
            return {"ok": True}

    def stop(self, name, exit_positions=False):
        with self.lock:
            r = self.runners[name]
            r.running = False
            r.state["auto_done"] = True                     # a manual stop is not undone by auto-start
            out = r.book.exit_all("stop") if exit_positions else []
            r.event("stop", exited=len(out))
            r.save_state()
            return {"ok": True, "exits": out}

    def set_params(self, name, changes: dict):
        with self.lock:
            r = self.runners[name]
            allowed = set(getattr(r.strategy, "params", {}) or {})
            bad = [k for k in changes if k not in allowed]
            if bad:
                return {"ok": False, "error": f"unknown params {bad}"}
            for k, v in changes.items():
                r.params[k] = type(r.strategy.params[k])(v) if r.strategy.params[k] is not None else v
            r.event("params", **changes)
            r.save_state()
            if self.persist:
                save_settings(name, params=r.params)        # carry to the next days
            return {"ok": True, "params": r.params}

    def set_auto(self, name, auto_start=None, auto_time=None):
        with self.lock:
            r = self.runners[name]
            if auto_time is not None:
                t = str(auto_time).strip()
                dt.time.fromisoformat(t)                    # ValueError on a bad time
                r.auto["auto_time"] = t[:5]
            if auto_start is not None:
                r.auto["auto_start"] = bool(auto_start)
            r.event("auto", **r.auto)
            if self.persist:
                save_settings(name, **r.auto)
            return {"ok": True, **r.auto}

    def reset(self, name, start=True):
        """Wipe today's PAPER book for one instrument and (optionally) start it fresh.
        Nothing is deleted: today's files move to data/books/{date}/_reset_{HHMMSS}/ (history too)."""
        import shutil
        with self.lock:
            r = self.runners[name]
            stamp = dt.datetime.now(IST).strftime("%H%M%S")
            moved = []
            if self.persist:
                arch = C.DATA / "books" / r.date / f"_reset_{stamp}"
                arch.mkdir(parents=True, exist_ok=True)
                for suffix in (".json", "_trades.jsonl", "_events.jsonl", "_state.json"):
                    p = C.DATA / "books" / r.date / f"{name}{suffix}"
                    if p.exists():
                        shutil.move(str(p), str(arch / p.name))
                        moved.append(p.name)
                h = C.DATA / "history" / r.date / f"{name}.jsonl"
                if h.exists():
                    shutil.move(str(h), str(arch / f"{name}_history.jsonl"))
                    moved.append(h.name)
            nr = self._attach(Runner(name, self.market, r.strategy_name, date=r.date, persist=self.persist))
            nr.state["auto_done"] = True                     # reset = owner's action; no auto re-trigger
            self.runners[name] = nr
            self.hist[name] = []
            nr.event("reset", archived=moved, folder=f"_reset_{stamp}")
            nr.save_state()
            out = {"ok": True, "archived": moved}
            if start:
                out["start"] = self.start(name, how="reset")
            return out

    def maybe_auto_start(self, n, r, now):
        """Start once a day at auto_time (Mon-Fri, inside the session, before square-off)."""
        if r.running or r.square_off_done or not r.auto.get("auto_start") or r.state.get("auto_done"):
            return
        if now.weekday() >= 5:
            return
        o, _close, sq = session(n, now.date())
        at = dt.datetime.combine(now.date(), dt.time.fromisoformat(r.auto.get("auto_time") or "09:05"), IST)
        if max(o, at) <= now < sq:
            self.start(n, how="auto")
            r.save_state()

    def manual_order(self, name, strike, kind, side, lots, expiry_n=None):
        with self.lock:
            r = self.runners[name]
            e = self.market.expiry(r.und, r.expiry_n if expiry_n is None else int(expiry_n))
            c = self.market.contract(r.und, e, float(strike), kind.upper())
            if not c:
                return {"ok": False, "error": f"no contract {r.und} {e} {strike} {kind}"}
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
                self.maybe_auto_start(n, r, now)
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
        if time.time() - self._last_st >= STS.SAMPLE_S:
            self._last_st = time.time()
            try:
                self.st_sample(now)
            except Exception:
                log.exception("Supertrend sample")
        if time.time() - self._last_sample >= C.HISTORY_EVERY_S:
            self._last_sample = time.time()
            self.sample(now)

    def sample(self, now):
        d = C.DATA / "history" / now.date().isoformat()
        if self.persist:
            d.mkdir(parents=True, exist_ok=True)
        for n, r in self.runners.items():
            has_book = any(p.lots or p.realized for p in r.book.positions.values())
            if not (r.running or has_book):
                continue
            m, im = r.book.mtm(), r.book.imbalance()
            u = r.und
            e = self.market.expiry(u, r.expiry_n)
            atm = r.state.get("atm") or self.market.atm(u, e)
            row = {"t": round(time.time(), 2), "mtm": m["total"], "imb": im["pct"],
                   "ce": im["ce_value"], "pe": im["pe_value"], "ref": self.market.ref_price(u, e), "atm": atm}
            if C.is_variant(n):                              # Supertrend variant: its signal ATM + ST line
                s = r.state.get("st") or {}
                atm = s.get("atm") or atm
                row["atm"] = atm
                if s.get("st") is not None:
                    row["st"], row["st_red"] = s["st"], s.get("trend") == "red"
            if atm and e:                                    # ATM premium chart: ATM CE / PE price
                for kind, f in (("CE", "ace"), ("PE", "ape")):
                    c = self.market.contract(u, e, atm, kind)
                    px = self.market.mark(c.key) if c else 0.0
                    row[f] = px if px > 0 else None
            sp = self.spreads(u, r, e, atm)                    # bid-ask gap % of the 6 strategy strikes
            if sp:
                row["sp"] = sp
            self.hist.setdefault(n, []).append(row)
            del self.hist[n][:-200000]
            if self.persist:
                with open(d / f"{n}.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")

    def spreads(self, n, r, e, atm):
        """Bid-ask gap as % of mid for ATM CE, CE+1, CE+2, ATM PE, PE-1, PE-2 (owner 4-Nov: spread chart).
        None for a strike with no two-sided quote. [] if ATM / step unknown."""
        step = float(r.params.get("step") or C.STRIKE_STEP.get(n) or 0)
        if not (atm and e and step):
            return []
        out = []
        for kind, sgn in (("CE", 1), ("PE", -1)):
            for i in range(3):
                c = self.market.contract(n, e, atm + sgn * i * step, kind)
                q = self.market.quote(c.key) if c else None
                if q and q.bid and q.ask and q.bid > 0 and q.ask >= q.bid:
                    out.append(round((q.ask - q.bid) / ((q.ask + q.bid) / 2) * 100, 2))
                else:
                    out.append(None)
        return out

    def rollover(self, date):
        """New day: fresh paper books (no carry), runners stopped."""
        with self.lock:
            for n, r in list(self.runners.items()):
                if any(p.lots for p in r.book.positions.values()):
                    r.event("rollover_with_open_positions_dropped")
                self.runners[n] = self._attach(Runner(n, self.market, r.strategy_name, date=date, persist=self.persist))
                self.hist[n] = []
        log.info("day rollover -> %s", date)

    def run_timer(self):
        def loop():
            while not self._stop.wait(0.5):
                try:
                    self.tick_timer()
                except Exception:
                    log.exception("timer")
        threading.Thread(target=loop, daemon=True, name="CepeTimer").start()

    def stop_all(self):
        self._stop.set()
        if self.quotes:
            self.quotes.stop()

    # ── read model for the API ──────────────────────────────────────────────
    def summary(self) -> dict:
        now = dt.datetime.now(IST)
        out = []
        for n, r in self.runners.items():
            u = r.und
            o, close, sq = session(n, now.date())
            e = self.market.expiry(u, r.expiry_n)
            rk = self.market.ref_key(u, e)
            rq = self.market.quote(rk) if rk else None
            out.append({"name": n, "und": u, "variant": "st" if C.is_variant(n) else "normal",
                        "label": C.label_of(n), "kind": C.UNDERLYINGS[u]["kind"], "running": r.running,
                        "strategy": r.strategy_name, "expiry": e, "lot": self.u.underlyings[u].lot,
                        "st": r.state.get("st") if C.is_variant(n) else None,
                        "ref": rq.ltp if rq else None, "ref_age": round(time.time() - rq.recv_ts, 1) if rq else None,
                        "atm": self.market.atm(u, e), "synthetic": self.market.synthetic(u, e), "mtm": r.book.mtm(), "imbalance": r.book.imbalance(),
                        "open_positions": sum(1 for p in r.book.positions.values() if p.lots),
                        "trades": len(r.book.trades), "errors": r.errors, "last_error": r.last_error,
                        "session": {"open": o.strftime("%H:%M"), "close": close.strftime("%H:%M"),
                                    "square_off": sq.strftime("%H:%M"), "in_session": o <= now < close},
                        "square_off_done": r.square_off_done, "phase": r.state.get("phase"),
                        "strat_atm": r.state.get("atm"), "blocked": r.state.get("blocked"),
                        "params": r.params, "step": float(r.params.get("step") or C.STRIKE_STEP.get(u) or 0),
                        "auto_start": r.auto.get("auto_start"), "auto_time": r.auto.get("auto_time"),
                        "auto_done": bool(r.state.get("auto_done"))})
        return {"now": now.isoformat(timespec="seconds"), "underlyings": out,
                "feed": self.feed.status() if self.feed else None, "universe_date": self.u.file_date}
