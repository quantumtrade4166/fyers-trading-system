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

from live_trading_options.cepe_imbalance.histbuf import HistBuf
from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import st_signal as STS
from live_trading_options.cepe_imbalance import strategy as S
from live_trading_options.cepe_imbalance.book import IST, PaperBook, atomic_write, today
from live_trading_options.cepe_imbalance.quotes import QuoteRecorder
from live_trading_options.cepe_imbalance.writer import W

log = logging.getLogger("cepe.engine")


# ── durable per-instrument settings (survive day rollover + restarts) ───────
# data/settings.json = {name: {"params": {...}, "auto_start": bool, "auto_time": "HH:MM"}}
_settings_lock = threading.Lock()


def settings_path(name=None):
    """17-Nov: the LIVE books keep their settings in their own file (the live engine is its own process;
    two processes rewriting one file could lose a change)."""
    return C.DATA / ("settings_live.json" if name and C.is_live(name) else "settings.json")


def load_settings(name=None) -> dict:
    p = settings_path(name)
    if not p.exists() and name and C.is_live(name):
        p = settings_path()                                  # first run: the values saved before the split
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        log.exception("%s unreadable; using defaults", p.name)
        return {}


def save_settings(name, **fields):
    with _settings_lock:
        doc = load_settings(name)
        if C.is_live(name):
            doc = {k: v for k, v in doc.items() if C.is_live(k)}
        doc.setdefault(name, {}).update(fields)
        p = settings_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(p, json.dumps(doc, indent=1))


def default_auto(name) -> dict:
    if C.is_syn(name):                                   # owner 23-Nov: synthetic to-and-fro, entry time editable
        return {"auto_start": C.LIVE_AUTO_DEFAULT if C.is_live(name) else C.AUTO_START_DEFAULT,
                "auto_time": C.SYN_AUTO_TIME}
    if C.is_live(name):                                  # owner 16-Nov: 09:16, and only while ARMED
        return {"auto_start": C.LIVE_AUTO_DEFAULT, "auto_time": C.LIVE_AUTO_TIME}
    u = C.und_of(name)
    times = C.ST_AUTO_START_TIME if C.is_variant(name) else C.AUTO_START_TIME
    return {"auto_start": C.AUTO_START_DEFAULT, "auto_time": times.get(u) or times[C.kind_of(name)]}


# ── session hours ───────────────────────────────────────────────────────────
def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def us_dst(day: dt.date) -> bool:
    """US daylight saving: 2nd Sunday of March -> 1st Sunday of November."""
    return _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)


def session(name: str, day: dt.date) -> tuple:
    """(open, close, square-off) of the TRADING DAY `day`. BTC full expiry opens the evening before."""
    kind = C.kind_of(name)
    t = lambda d, s: dt.datetime.combine(d, dt.time.fromisoformat(s), IST)
    if kind == "crypto":
        cs = C.CRYPTO_SESSION[C.und_of(name)]
        close = t(day, cs["close"])
        opened = t(day - dt.timedelta(days=1) if cs["overnight"] else day, cs["open"])
        return opened, close, close - dt.timedelta(minutes=C.SQUARE_OFF_BEFORE_MIN[kind])
    if kind == "index":
        o, c = C.NSE_HOURS
    else:
        o, c = C.MCX_HOURS_US_DST if us_dst(day) else C.MCX_HOURS_US_STD
    close = t(day, c)
    return t(day, o), close, close - dt.timedelta(minutes=C.SQUARE_OFF_BEFORE_MIN[kind])



LATEST_SYN_EXIT_BEFORE_CLOSE_S = 30          # a Synthetic / Premium book may run to 30 s before the close


def square_off_at(name: str, r, sq: dt.datetime, close: dt.datetime) -> dt.datetime:
    """25-Nov (owner): a Synthetic / Premium book with an exit time AFTER the engine's 15:15 square-off keeps
    running until that exit time (owner set 15:29 and was squared off at 15:15). Capped 30 s before close."""
    if not C.is_syn(name):
        return sq
    try:
        t = dt.time.fromisoformat(str((r.params or {}).get("exit_time") or "").strip())
    except (ValueError, AttributeError):
        return sq
    want = dt.datetime.combine(sq.date(), t, sq.tzinfo)
    return min(max(sq, want), close - dt.timedelta(seconds=LATEST_SYN_EXIT_BEFORE_CLOSE_S))

def trading_day(name: str, now: dt.datetime | None = None) -> dt.date:
    """Calendar date for everything but BTC full expiry, whose day D runs 17:30 (D-1) -> 17:30 D: from
    17:30 its book, files and P&L belong to TOMORROW (the expiry it trades)."""
    now = now or dt.datetime.now(IST)
    if C.kind_of(name) == "crypto":
        cs = C.CRYPTO_SESSION[C.und_of(name)]
        if cs["overnight"] and now.strftime("%H:%M") >= cs["close"]:
            return now.date() + dt.timedelta(days=1)
    return now.date()


def book_date(name: str, now: dt.datetime | None = None) -> str:
    return trading_day(name, now).isoformat()


def every_day(name: str) -> bool:
    """Crypto trades 7 days a week (owner 14-Nov: BTC auto-starts on weekends too)."""
    return C.kind_of(name) == "crypto"


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
        o, _close, sq = session(self.name, trading_day(self.name, now))
        sq = square_off_at(self._r.name, self._r, sq, _close)      # 25-Nov: a Synthetic book's own later exit time
        return o <= now < sq


class Runner:
    def __init__(self, name, market, strategy_name=None, date=None, persist=True, book_factory=None):
        self.name = name                    # book name: "NIFTY" or the Supertrend variant "NIFTY_ST"
        self.und = C.und_of(name)
        if not strategy_name and C.is_syn(name):                     # owner 23-Nov: synthetic to-and-fro
            strategy_name = "syn_tofro_v2" if C.syn_version(name) == 2 else "syn_tofro"   # 25-Nov: V2 = Premium
        if not strategy_name and C.is_flex(name):                    # owner 17-Nov: NIFTY / SENSEX flex rules
            strategy_name = "cepe_st_flex" if C.is_variant(name) else "cepe_flex"
        strategy_name = strategy_name or ({(True, False): "cepe_st", (False, True): "cepe_new",
                                           (True, True): "cepe_st_new"}.get((C.is_variant(name), C.is_new_rules(name)))
                                          or S.DEFAULT)
        self.market = market
        self.persist = persist
        self.date = date or book_date(name)
        self.book = (book_factory or PaperBook)(name, market, self.date, persist=persist)
        self.live = bool(getattr(self.book, "live", False))
        self.strategy_name = strategy_name
        self.strategy = S.REGISTRY[strategy_name]()
        self.params = dict(getattr(self.strategy, "params", {}) or {})
        if C.is_live(name):
            self.params.update(C.LIVE_DEFAULT_PARAMS)    # 1 lot, max loss Rs 5,000 (until changed on screen)
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
            saved = load_settings(name).get(name, {})
            self.params.update({k: v for k, v in (saved.get("params") or {}).items() if k in self.params})
            self.auto.update({k: saved[k] for k in ("auto_start", "auto_time") if k in saved})
            self._load_state()

    # ── persistence ─────────────────────────────────────────────────────────
    def _state_path(self):
        d = C.DATA / "books" / self.date
        if W.started and not self.live:                      # the writer makes the folder: no disk call here
            return d / f"{self.name}_state.json"             # (the rules save state on almost every tick)
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
        text = json.dumps({"running": self.running, "square_off_done": self.square_off_done,
                           "strategy": self.strategy_name, "params": self.params,
                           "expiry_n": self.expiry_n, "state": self.state}, indent=1, default=str)
        if self.live:                                        # live book: on disk before anything else happens
            atomic_write(p, text)
        else:
            W.replace(p, text)                               # paper: writer thread (at once when not started)

    def event(self, event, **fields):
        e = {"ts": time.time(), "time": dt.datetime.now(IST).strftime("%H:%M:%S"), "event": event, **fields}
        self.events.append(e)
        del self.events[:-500]
        if self.persist:
            p = C.DATA / "books" / self.date / f"{self.name}_events.jsonl"
            if self.live or not W.started:
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(e, default=str) + "\n")
            else:
                W.append(p, json.dumps(e, default=str) + "\n")

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
    def __init__(self, universe, market, feed=None, persist=True, live=None, role="all"):
        self.u = universe
        self.market = market
        self.feed = feed
        self.persist = persist
        self.live = live                    # live.LiveCtx (Kotak D) or None: then NO live books exist at all
        self.role = role                    # "paper" / "live" = one of the two processes (config.ROLE)
        self.lock = threading.RLock()
        # books: one normal + one Supertrend variant ("NIFTY_ST") per underlying (+ the LIVE books)
        self.runners = {}
        for n in C.ALL_BOOKS:
            if C.und_of(n) in universe.underlyings and (live is not None or not C.is_live(n))                     and C.book_in_role(n, role) and (role != "live" or C.und_of(n) in C.LIVE_BOOKS):
                self.runners[n] = self._runner(n)
        self.unds = [n for n in C.ORDER if n in universe.underlyings]
        self.signals = {u: STS.StSignal(u) for u in self.unds}
        self.routes: dict[str, set] = {}          # feed key -> {book names}
        self._stop = threading.Event()
        self._last_sample = 0.0
        self._last_st = 0.0
        self._last_subs = 0.0
        self.started_at = time.time()
        self.hist: dict[str, HistBuf] = {n: HistBuf() for n in self.runners}   # today's chart samples (compact)
        # every tick, for exact replays (the paper engine records NIFTY / SENSEX; the live engine must not
        # write the same files from a second process)
        self.quotes = QuoteRecorder(universe) if persist and role != "live" else None
        # LIVE books (16-Nov fix): a Kotak order takes seconds (send, poll, re-price). Run under the engine
        # lock on the feed thread, it froze EVERY book and every price/MTM until Kotak answered. Each live
        # book now has its own lock + worker thread; ticks only queue keys for it (live_async, set by
        # run_timer). Tests leave live_async off -> everything runs inline, as before.
        self.live_async = False
        self.live_locks = {n: threading.RLock() for n in self.runners if C.is_live(n)}
        self._live_keys = {n: set() for n in self.live_locks}
        self._live_wake = {n: threading.Event() for n in self.live_locks}
        self._live_klock = threading.Lock()
        self.live_busy: dict = {}                 # name -> time the worker began its current step
        self.live_loop_at: dict = {}              # name -> last time its worker loop went round (heartbeat)
        if persist:
            self._load_hist()

    def lock_for(self, name):
        """The lock guarding one book: its own for a live book (async), else the engine lock."""
        return self.live_locks.get(name) if (self.live_async and name in self.live_locks) else self.lock

    def _runner(self, name, strategy_name=None, date=None):
        """A Runner; a "{NAME}_LIVE" book trades real orders on Kotak D (live.KotakLiveBook)."""
        bf = None
        if C.is_live(name):
            from live_trading_options.cepe_imbalance.live import KotakLiveBook
            bf = lambda n, m, d, persist: KotakLiveBook(n, m, self.live, d, persist)
        return self._attach(Runner(name, self.market, strategy_name, date=date, persist=self.persist, book_factory=bf))

    # ── live guard: an order that could not be completed -> flatten + stop the book ──
    def live_guard(self, r):
        b = r.book
        if not r.live or not b.fault or r.state.get("phase") == "halted":
            return
        r.event("live_fault", fault=b.fault)
        log.error("%s LIVE FAULT: %s -> exit all + stop", r.name, b.fault)
        out = b.exit_all("FAULT: " + str(b.fault)[:120])
        r.state["phase"] = "halted"
        r.state["blocked"] = "LIVE halted: " + str(b.fault)[:200]
        r.running = False
        r.event("live_halted", exits=len(out), pending_exit=bool(b.pending_exit))
        r.save_state()

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
            td = trading_day(u, now)
            day = td.isoformat()
            o, close, _sq = session(u, td)
            if sig.day != day or sig.expiry != e:            # new day / expiry: fresh candles + warm-up
                sig.reset(day, e)
                sig.warm_queued = False
            # crypto (24/7): warm up AT the open on the minutes just before it, not hours early at midnight
            if e and not getattr(sig, "warm_queued", False) and                     (C.kind_of(u) != "crypto" or o <= now < close):
                sig.warm_queued = True
                self.start_warmup(u, day, e, step, buf)
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

    def _load_hist(self):
        for n, r in self.runners.items():
            p = C.DATA / "history" / r.date / f"{n}.jsonl"
            buf = HistBuf()
            if p.exists():
                with open(p, encoding="utf-8") as fh:    # line by line: never the whole day as dicts at once
                    for x in fh:
                        if x.strip():
                            try:
                                buf.append(json.loads(x))
                            except (ValueError, KeyError):
                                pass                        # a half-written last line after a crash
            self.hist[n] = buf

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

    def feed_wanted(self, name) -> bool:
        """Live engine (17-Nov): subscribe only the index that is ARMED, running or still holds positions -
        the owner trades NIFTY or SENSEX on a given day, so the feed carries ~60 strikes, not ~1,100."""
        if self.role != "live":
            return True
        r = self.runners[name]
        armed = bool(self.live and self.live.control.armed(name))
        return armed or r.running or any(p.lots for p in r.book.positions.values())

    def refresh_subscriptions(self) -> int:
        new = 0
        for name in self.runners:
            if not self.feed_wanted(name):
                continue
            keys = self.wanted_keys(name)
            for k in keys:
                self.routes.setdefault(k, set()).add(name)
            if self.feed:
                new += self.feed.want(keys)
        return new

    # ── feed callback ───────────────────────────────────────────────────────
    def on_ticks(self, ticks):
        # ticks = one batch from the feed worker (many frames when busy); every tick is recorded as it is
        # applied, then each touched book is evaluated ONCE for the whole batch.
        rec, routes, und_of = self.quotes, self.routes, C.und_of

        def snap(k, q):
            names = routes.get(k)
            if names:                                           # once per UNDERLYING, not per book
                for u in {und_of(n) for n in names}:
                    rec.add(u, k, q, q.recv_ts or None)

        keys = self.market.apply(ticks, on_quote=snap if rec else None)
        touched: dict[str, list] = {}
        for k in dict.fromkeys(keys):
            for n in routes.get(k, ()):
                touched.setdefault(n, []).append(k)
        if not touched:
            return
        if self.live_async:                                     # live books: hand the keys to their worker
            for n in [n for n in touched if n in self.live_locks]:
                with self._live_klock:
                    self._live_keys[n].update(touched.pop(n))
                self._live_wake[n].set()
        with self.lock:
            for n, ks in touched.items():
                r = self.runners[n]
                if r.running and not r.square_off_done:
                    r.call(r.strategy.on_tick, ks)
                    self.live_guard(r)

    # ── controls ────────────────────────────────────────────────────────────
    def start(self, name, how="manual"):
        with self.lock_for(name):
            r = self.runners[name]
            if r.live:
                if not self.live.control.armed(name):
                    return {"ok": False, "error": "LIVE is not armed - arm it first"}
                if r.book.pending_exit:
                    return {"ok": False, "error": "an exit is still being retried - wait until flat"}
                if r.book.fault and not r.book.clear_fault():
                    return {"ok": False, "error": f"book not flat / orders unsettled: {r.book.fault}"}
            r.state["auto_done"] = True                     # any start/stop today ends auto-start for today
            if r.running:
                return {"ok": True, "note": "already running"}
            r.running, r.square_off_done = True, False
            r.state["money_base"] = r.book.mtm()["total"]  # owner 18-Nov: target / lock count from this Start
            r.state["mtm_peak"] = None
            r.event("start", strategy=r.strategy_name, how=how)
            if r.live and r.state.get("phase") in ("halted", "stopped", "done"):
                r.state["phase"] = None                     # a fresh Start after a halt / stop
                r.state["blocked"] = None
            if r.live and self.live_async:                  # entry orders run on the book's worker, not here
                r.pending_start = True
                if r.state.get("phase") not in ("in", "wait_entry"):    # a restart before the worker ran
                    r.state["phase"] = "in" if any(p.lots for p in r.book.positions.values()) else "wait_entry"
                r.save_state()
                self._live_wake[name].set()
                return {"ok": True, "note": "starting - entry orders go out now"}
            r.call(r.strategy.on_start)
            self.live_guard(r)
            r.save_state()
            return {"ok": True}

    def stop(self, name, exit_positions=False):
        r0 = self.runners[name]
        r0.running = False                                  # at once: a live worker mid-step stops after it
        with self.lock_for(name):
            r = self.runners[name]
            r.running = False
            r.pending_start = False
            r.state["auto_done"] = True                     # a manual stop is not undone by auto-start
            out = r.book.exit_all("stop") if exit_positions else []
            if exit_positions and not any(p.lots for p in r.book.positions.values()):
                r.state["phase"] = "exited"                 # 17-Nov: flat after Stop & exit all (was left "in")
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
            if r.live and not (1 <= int(changes.get("lots", 1)) <= C.LIVE_MAX_LOTS):
                return {"ok": False, "error": f"live lots must be 1..{C.LIVE_MAX_LOTS}"}
            old_lots = r.strategy.unit(r.ctx) if any(p.lots for p in r.book.positions.values()) else None
            for k, v in changes.items():
                r.params[k] = type(r.strategy.params[k])(v) if r.strategy.params[k] is not None else v
            out = {}
            # owner 17-Nov: a lots change while IN TRADE resizes the open book (every strike scaled, CE / PE
            # pairs); flat books just use the new lots at the next entry
            new_lots = int(r.params.get("lots") or 1)
            if old_lots and "lots" in changes and new_lots != old_lots:
                r.state["resize_to"] = new_lots
                out["resize"] = f"{old_lots} -> {new_lots} lots on the open book: orders go out now"
            elif "lots" in changes and r.state.get("resize_to"):
                r.state["resize_to"] = None if new_lots == old_lots else new_lots
            r.event("params", **changes)
            r.save_state()
            if self.persist:
                save_settings(name, params=r.params)        # carry to the next days
            if r.state.get("resize_to") and r.live and self.live_async:
                self._live_wake[name].set()                 # the live worker resizes at once
            return {"ok": True, "params": r.params, "applied_at": dt.datetime.now(IST).strftime("%H:%M:%S"), **out}

    def set_auto(self, name, auto_start=None, auto_time=None):
        with self.lock:
            r = self.runners[name]
            if auto_time is not None:
                t = str(auto_time).strip()
                tm = dt.time.fromisoformat(t)               # ValueError on a bad time
                # owner 17-Nov: to the SECOND ("09:15:20"); "09:15" still means 09:15:00
                r.auto["auto_time"] = tm.strftime("%H:%M:%S") if tm.second else tm.strftime("%H:%M")
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
            if r.live:
                return {"ok": False, "error": "a LIVE book is never reset (its file is the record of real orders)"}
            stamp = dt.datetime.now(IST).strftime("%H%M%S")
            moved = []
            W.flush()                                        # everything of this book on disk before it moves
            W.forget()                                       # ...and the files that move away get written again
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
            nr = self._runner(name, r.strategy_name, date=r.date)
            nr.state["auto_done"] = True                     # reset = owner's action; no auto re-trigger
            self.runners[name] = nr
            self.hist[name] = HistBuf()
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
        if r.live and not self.live.control.armed(n):       # live auto-start only while ARMED
            return
        if now.weekday() >= 5 and not every_day(n):
            return
        o, _close, sq = session(n, trading_day(n, now))
        at = dt.datetime.combine(o.date(), dt.time.fromisoformat(r.auto.get("auto_time") or "09:05"), IST)
        if at < o:                                       # BTC full expiry: an auto time after midnight
            at += dt.timedelta(days=1)
        if max(o, at) <= now < sq:
            self.start(n, how="auto")
            r.save_state()

    def manual_order(self, name, strike, kind, side, lots, expiry_n=None):
        with self.lock:
            r = self.runners[name]
            if r.live:
                return {"ok": False, "error": "manual orders are paper-only (the live book trades its rules only)"}
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
        for n, r in list(self.runners.items()):          # per book: BTC full expiry rolls at 17:30, not midnight
            if r.date != book_date(n, now):
                self.rollover(book_date(n, now), only=n)
        if time.time() - self._last_subs >= 5:
            self._last_subs = time.time()
            self.refresh_subscriptions()
        with self.lock:
            for n, r in self.runners.items():
                if self.live_async and n in self.live_locks:
                    continue                                 # its own worker runs it (live_step)
                self.maybe_auto_start(n, r, now)
                if r.live:
                    self.live_timer(n, r, now)
                if not r.running or r.square_off_done:
                    continue
                o, close, sq = session(n, trading_day(n, now))
                sq = square_off_at(n, r, sq, close)
                if now >= sq:
                    r.square_off_done = True
                    r.event("session_end_square_off")
                    r.call(r.strategy.on_session_end)
                    r.save_state()
                elif now >= o:
                    r.call(r.strategy.on_timer)
                self.live_guard(r)
        if time.time() - self._last_st >= STS.SAMPLE_S:
            self._last_st = time.time()
            try:
                self.st_sample(now)
            except Exception:
                log.exception("Supertrend sample")
        if time.time() - self._last_sample >= C.HISTORY_EVERY_S:
            self._last_sample = time.time()
            self.sample(now)

    # ── live book worker: its ticks, timer and orders, off the feed thread and the engine lock ──
    def live_step(self, n, timer=True, now=None):
        with self.live_locks[n]:
            r = self.runners.get(n)
            if r is None:
                return
            now = now or dt.datetime.now(IST)
            with self._live_klock:
                keys, self._live_keys[n] = self._live_keys[n], set()
            if r.date != book_date(n, now):
                return                                       # the timer thread rolls the day over first
            self.live_busy[n] = time.time()
            try:
                if timer:
                    self.maybe_auto_start(n, r, now)
                    self.live_timer(n, r, now)
                if getattr(r, "pending_start", False):
                    r.pending_start = False
                    if r.running:
                        r.call(r.strategy.on_start)
                        self.live_guard(r)
                        r.save_state()
                        keys = set()                         # on_start just evaluated on the newest prices
                if r.running and not r.square_off_done:
                    o, _close, sq = session(n, trading_day(n, now))
                    sq = square_off_at(n, r, sq, _close)
                    if timer and now >= sq:
                        r.square_off_done = True
                        r.event("session_end_square_off")
                        r.call(r.strategy.on_session_end)
                        r.save_state()
                    elif keys:
                        r.call(r.strategy.on_tick, sorted(keys))
                    elif timer and now >= o:
                        r.call(r.strategy.on_timer)
                    self.live_guard(r)
            finally:
                self.live_busy.pop(n, None)

    def start_live_workers(self):
        self.live_async = True

        def loop(n):
            wake, last = self._live_wake[n], 0.0
            while not self._stop.is_set():
                wake.wait(0.5)
                wake.clear()
                t = time.time()
                self.live_loop_at[n] = t                     # heartbeat: the live card shows its age
                try:
                    self.live_step(n, timer=t - last >= 0.5)
                except Exception:
                    log.exception("%s live worker", n)
                    time.sleep(0.5)
                if t - last >= 0.5:
                    last = t
        for n in self.live_locks:
            threading.Thread(target=loop, args=(n,), daemon=True, name=f"CepeLive-{n}").start()

    def live_timer(self, n, r, now):
        """Live books: retry an unfinished exit; square off at square-off time even if stopped by hand."""
        b = r.book
        try:
            # 23-Nov fix: orders Kotak could not confirm are re-read every few seconds, not only on the next
            # order. A fill found on a halted / stopped book is bought back at once (it must never sit alone).
            late = b.recheck_unsettled() if hasattr(b, "recheck_unsettled") else []
            if late:
                r.event("late_fill", fills=[f"{t['side']} {t['lots']:g} {t['symbol']} @{t['price']}" for t in late])
                log.error("%s LATE FILL found at Kotak: %s", n, [t["symbol"] for t in late])
                if (not r.running or b.fault or r.state.get("phase") in ("halted", "stopped", "done", "exited"))                         and any(p.lots for p in b.positions.values()):
                    out = b.exit_all("late fill on a halted book - closed at once")
                    r.state["blocked"] = (f"LATE FILL at Kotak ({', '.join(t['symbol'] for t in late)}) on a halted book"
                                          f" - bought back at once")
                    r.event("late_fill_exit", exits=len(out), pending_exit=bool(b.pending_exit))
                r.save_state()
            b.retry_exit()
            o, _close, sq = session(n, trading_day(n, now))
            sq = square_off_at(n, r, sq, _close)
            if now >= sq and not r.state.get("live_eod_done") and any(p.lots for p in b.positions.values()):
                r.state["live_eod_done"] = True
                r.event("live_eod_square_off", running=r.running)
                b.exit_all("session end (live safety)")
                r.save_state()
        except Exception:
            log.exception("%s live timer", n)

    def sample(self, now):
        for n, r in self.runners.items():
            d = C.DATA / "history" / r.date
            if self.persist and not W.started:
                d.mkdir(parents=True, exist_ok=True)
            has_book = any(p.lots or p.realized for p in r.book.positions.values())
            if not (r.running or has_book):
                continue
            m, im = r.book.mtm(), r.book.imbalance()
            u = r.und
            e = self.market.expiry(u, r.expiry_n)
            # owner 17-Nov: a running book charts ITS strategy ATM (the strike its rules / exit levels use);
            # a stopped book's ATM no longer updates, so its chart follows the market's ATM instead of freezing
            atm = (r.state.get("atm") if r.running else None) or self.market.atm(u, e)
            row = {"t": round(time.time(), 2), "mtm": m["total"], "imb": im["pct"],
                   "ce": im["ce_value"], "pe": im["pe_value"], "ref": self.market.ref_price(u, e), "atm": atm}
            if C.is_variant(n):                              # Supertrend variant: its signal ATM + ST line
                s = r.state.get("st") or {}
                atm = s.get("atm") or atm
                row["atm"] = atm
                if s.get("st") is not None:
                    row["st"], row["st_red"] = s["st"], s.get("trend") == "red"
            elif C.is_syn(n) and r.state.get("syn") is not None:
                row["st"] = r.state["syn"]                   # 23-Nov synthetic book: the "st" column = SYNTHETIC
            if atm and e:                                    # ATM premium chart: ATM CE / PE price
                for kind, f in (("CE", "ace"), ("PE", "ape")):
                    c = self.market.contract(u, e, atm, kind)
                    px = self.market.mark(c.key) if c else 0.0
                    row[f] = px if px > 0 else None
            sp = self.spreads(u, r, e, atm)                    # bid-ask gap % of the 6 strategy strikes
            if sp:
                row["sp"] = sp
            self.hist.setdefault(n, HistBuf()).append(row)
            self.hist[n].trim(200000)
            if self.persist:                                 # chart rows: display only, never urgent
                W.append(d / f"{n}.jsonl", json.dumps(row) + "\n", wake=False)

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

    def rollover(self, date, only=None):
        """New day: fresh paper books (no carry), runners stopped. only = one book (its own trading day)."""
        with self.lock:
            for n, r in list(self.runners.items()):
                if only and n != only:
                    continue
                if any(p.lots for p in r.book.positions.values()):
                    r.event("rollover_with_open_positions_dropped")
                if r.live and any(p.lots for p in r.book.positions.values()):
                    r.event("rollover_with_LIVE_positions_carried")      # the new book carries them in
                with self.live_locks.get(n) or self.lock:     # never swap a live book mid-order
                    self.runners[n] = self._runner(n, r.strategy_name, date=date)
                self.hist[n] = HistBuf()
        log.info("day rollover -> %s%s", date, f" ({only})" if only else "")

    def run_timer(self):
        def loop():
            while not self._stop.wait(0.5):
                self.timer_busy = time.time()                # read by the stall watchdog (diag.py)
                try:
                    self.tick_timer()
                except Exception:
                    log.exception("timer")
                self.timer_busy = 0.0
        threading.Thread(target=loop, daemon=True, name="CepeTimer").start()
        self.start_live_workers()

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
            o, close, sq = session(n, trading_day(n, now))
            e = self.market.expiry(u, r.expiry_n)
            rk = self.market.ref_key(u, e)
            rq = self.market.quote(rk) if rk else None
            out.append({"name": n, "und": u, "variant": ("syn2" if C.syn_version(n) == 2 else "syn") if C.is_syn(n) else "st_new" if C.is_variant(n) and C.is_new_rules(n) else "st" if C.is_variant(n)
                        else "new" if C.is_new_rules(n) else "normal",
                        "label": C.label_of(n), "kind": C.UNDERLYINGS[u]["kind"], "ccy": C.ccy_of(n),
                        "date": r.date, "running": r.running,
                        "strategy": r.strategy_name, "expiry": e, "lot": self.u.underlyings[u].lot,
                        "st": r.state.get("st") if C.is_variant(n) else None,
                        "ref": rq.ltp if rq else None, "ref_age": round(time.time() - rq.recv_ts, 1) if rq else None,
                        "atm": self.market.atm(u, e), "synthetic": self.market.synthetic(u, e), "mtm": r.book.mtm(), "imbalance": r.book.imbalance(),
                        "open_positions": sum(1 for p in r.book.positions.values() if p.lots),
                        "book_lots": r.strategy.unit(r.ctx) if any(p.lots for p in r.book.positions.values()) else None,
                        "resize_to": r.state.get("resize_to"), "atm_cp": r.state.get("atm_cp"),
                        "mtm_peak": r.state.get("mtm_peak"), "lock_floor": r.state.get("lock_floor"),
                        "trades": len(r.book.trades), "errors": r.errors, "last_error": r.last_error,
                        "session": {"open": o.strftime("%H:%M"), "close": close.strftime("%H:%M"),
                                    "square_off": sq.strftime("%H:%M"), "in_session": o <= now < close},
                        "square_off_done": r.square_off_done, "phase": r.state.get("phase"),
                        "strat_atm": r.state.get("atm"), "blocked": r.state.get("blocked"),
                        "syn": r.strategy.view(r.ctx) if hasattr(r.strategy, "view") else None,
                        "params": r.params, "step": float(r.params.get("step") or C.STRIKE_STEP.get(u) or 0),
                        "auto_start": r.auto.get("auto_start"), "auto_time": r.auto.get("auto_time"),
                        "auto_done": bool(r.state.get("auto_done")), "live": ({**r.book.live_view(), "busy_s": round(time.time() - self.live_busy[n], 1)
                                  if n in self.live_busy else None,
                                  "loop_age": round(time.time() - self.live_loop_at[n], 1) if n in self.live_loop_at else None,
                                  "stale_s": r.state.get("stale_s"), "engine": self.role,
                                  "subscribed": self.feed_wanted(n)} if r.live else None)})
        return {"now": now.isoformat(timespec="seconds"), "underlyings": out,
                "feed": self.feed.status() if self.feed else None, "universe_date": self.u.file_date}
