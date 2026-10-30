"""
mcx_terminal/server.py — the terminal's engine + web server (Kotak P, MCX options).
==================================================================================

Own process, own port (47680), own Kotak session. Never touches the dashboard or any other engine.
Orders: PAPER (paper.py) by default. LIVE (live.py, real Kotak P orders) only after the OWNER arms it on
the screen (typed confirmation, owner email only, resets every day). Live positions = own tagged orders only.

Speed path:  Kotak feed thread -> latest tick per token (dict) -> one flush scheduled on the asyncio
loop -> each screen gets only the ticks for tokens it is watching, as one compact JSON frame.
Ticks arriving while a flush is pending just overwrite the pending value (natural coalescing,
no fixed timer, nothing older than one loop turn).

    cd C:\\trading\\fyers_data_pipeline
    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.server
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.access import AccessGuard
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal.feed import KotakFeed
from live_trading_options.mcx_terminal.kotak_session import SessionHolder
from live_trading_options.mcx_terminal.paper import PaperBook
from live_trading_options.mcx_terminal.live import LiveBook, txt as _txt
from live_trading_options.mcx_terminal.orderfeed import OrderFeed
from live_trading_options.mcx_terminal.sl import AutoSL
from live_trading_options.mcx_terminal.sched import ExitTimers
from live_trading_options.mcx_terminal.risk import RiskGuard
from live_trading_options.mcx_terminal.settings import GAP_CHOICES, Settings
from live_trading_options.mcx_terminal.mtm import AtmHistory, MtmHistory, SAMPLE_S
from live_trading_options.mcx_terminal.recorder import DayRecorder, now_ms, REPLAY_DIR

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
log = logging.getLogger("mcx.server")
RESET_AT = dt.time(0, 5)          # after MCX close: new day = fresh books, guards, arm (everything resets)
REFRESH_AT = dt.time(8, 45)       # fresh Kotak login + today's contract list before MCX opens at 09:00


def next_restart(born: dt.datetime) -> dt.datetime:
    """The first reset/refresh moment after the engine was born."""
    d = born.date()
    cands = [dt.datetime.combine(d + dt.timedelta(days=k), t, tzinfo=IST)
             for k in (0, 1) for t in (RESET_AT, REFRESH_AT)]
    return min(x for x in cands if x > born)


def owner_users() -> set:
    """Who may arm LIVE: the owner's Cloudflare email(s), plus "local" (on the VPS itself / SSH tunnel)."""
    em = os.getenv("MCX_OWNER_EMAILS", "prakhar4sapa@gmail.com")
    return {"local"} | {e.strip().lower() for e in em.split(",") if e.strip()}    # fresh Kotak login + contract list before MCX opens at 09:00
REPLAY_BUILD_AT = dt.time(23, 58) # after MCX close (23:30 / 23:55): build the day's double-click replay file

try:
    from dotenv import load_dotenv
    load_dotenv(C.ROOT / "deployment" / ".env")       # MCX_ACCESS_* for the login lock
except Exception:
    pass


def _setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(C.LOG_DIR / "mcx_terminal.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


class Engine:
    def __init__(self):
        self.sessions = SessionHolder()
        self.universe: I.Universe | None = None
        self.last: dict[int, list] = {}            # token -> latest tick (12 fields, what screens get)
        self.depth: dict[int, tuple] = {}          # token -> (bids, asks) 5 levels, engine-only
        self.pending: dict[int, list] = {}
        self.flush_scheduled = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set["Client"] = set()
        self.feed: KotakFeed | None = None
        self.paper: PaperBook | None = None
        self.live: LiveBook | None = None
        self.sls = None
        self.risks: dict = {}                      # "PAPER"/"LIVE" -> RiskGuard (both always watching)
        self.mode = "PAPER"                        # which book the screen trades into
        self.arm: dict = {}                        # who armed LIVE today, and when
        self.settings = Settings()
        self.timers: ExitTimers | None = None      # exit-by-time (per instrument), fired by housekeeping
        day = dt.datetime.now(IST).date().isoformat()
        self.mtms = {"PAPER": MtmHistory(), "LIVE": MtmHistory(path=C.DATA / f"mtm_live_{day}.json")}
        self.atm_hist = AtmHistory()
        self.book_tokens: set[int] = set()         # tokens with an open position / open order
        self.rec: DayRecorder | None = None        # day recorder for the replay files (always on)
        self.chain_defs: dict = {}                 # (name, expiry) -> chain definition sent to screens
        self._rec_last: dict = {}                  # token -> last recorded tick (skip unchanged repeats)
        self.margin: dict = {"ok": False, "msg": "loading"}   # Kotak P limits, refreshed every 30 s
        self._atm_at = 0.0                         # last ATM-straddle broadcast (once a second)
        self.started_at = time.time()
        self.error = ""
        self._plock = threading.Lock()

    @property
    def book(self):
        return self.live if self.mode == "LIVE" else self.paper

    @property
    def risk(self):
        return self.risks.get(self.mode)

    @property
    def mtm(self):
        return self.mtms[self.mode]

    # The day recorder (replay file) follows the LIVE book ONLY: live orders/positions, live MTM curve,
    # live risk guard. Paper trading never appears in it and never starts it.
    def _rec_book(self) -> dict:
        return {"t": "book", **self.live.snapshot(), "sl": self.sls["LIVE"].view() if self.sls else {}}

    def book_msg(self) -> dict:
        snap = self.book.snapshot()
        self._enrich(snap["positions"])
        return {"t": "book", **snap, "sl": self.sls[self.mode].view() if self.sls else {}}

    def away_of(self, token, away: bool = True):
        """The contract one strike GAP further out of the money (away: CE up, PE down) or one gap
        toward the money (away=False: CE down, PE up), same expiry."""
        c = self.universe.by_token.get(token)
        if c is None or c.kind not in ("CE", "PE"):
            return None
        chain = self.universe.products[c.name].chains.get(c.expiry, {})
        gap = ((self.settings.get().get("strike_gap") or {}).get(c.name)) or 0
        up = (c.kind == "CE") == away
        if gap:
            k = c.strike + (gap if up else -gap)
        else:
            ks = sorted(chain)
            i = ks.index(c.strike) + (1 if up else -1)
            k = ks[i] if 0 <= i < len(ks) else None
        leg = (chain.get(k) or {}).get(c.kind) if k is not None else None
        return leg

    def _enrich(self, positions):
        """Columns the Positions table shows: index, strike, C/P, expiry, product, away contract."""
        prod_default = self.settings.get().get("product", "NRML")
        for p in positions:
            c = self.universe.by_token.get(p["token"])
            if c is None:
                continue
            p.update(strike=c.strike, kind=c.kind, expiry=c.expiry)
            bp = p.get("by_prod") or {}
            nz = sorted(((k, v) for k, v in bp.items() if v), key=lambda kv: -abs(kv[1]))
            p["product"] = nz[0][0] if nz else (next(iter(bp), None) or prod_default)
            p.pop("cf", None)
            for key, out in (("away", True), ("close", False)):
                a = self.away_of(c.token, out) if p.get("net") else None
                if a is not None:
                    p[key] = {"token": a.token, "strike": a.strike, "symbol": a.symbol}

    def _away_tokens(self) -> set:
        out = set()
        for bk in (self.paper, self.live):
            if bk is None:
                continue
            with bk.lock:
                toks = [t for t, p in bk.pos.items() if p.get("net")]
            for t in toks:
                for way in (True, False):
                    a = self.away_of(t, way)
                    if a is not None:
                        out.add(a.token)
        return out

    @property
    def sl(self):
        return self.sls[self.mode]

    def _rec_status(self) -> dict:
        return {"t": "status", **self.status(), "mode": "LIVE"}

    # ── LIVE arm (owner only) ───────────────────────────────────────────────
    def _arm_path(self):
        return C.DATA / f"live_arm_{dt.datetime.now(IST).date().isoformat()}.json"

    def _load_arm(self):
        """Armed only for the day it was armed: every new day starts in PAPER."""
        try:
            p = self._arm_path()
            if p.exists():
                a = json.loads(p.read_text(encoding="utf-8"))
                if a.get("armed"):
                    self.arm, self.mode = a, "LIVE"
                    log.warning("LIVE re-armed from today's arm file (armed by %s at %s)", a.get("by"), a.get("at"))
        except Exception:
            log.exception("arm file")

    def set_mode(self, mode: str, user: str, confirm: str = "") -> dict:
        mode = str(mode).upper()
        if mode not in ("PAPER", "LIVE"):
            return {"ok": False, "msg": "mode must be PAPER or LIVE"}
        if mode == "LIVE":
            if user not in owner_users():
                log.warning("LIVE arm refused for %s (not the owner)", user)
                return {"ok": False, "msg": "only the owner can arm LIVE trading"}
            if str(confirm).strip().upper() != "LIVE":
                return {"ok": False, "msg": "type LIVE to arm real Kotak P orders"}
            if not self.live.broker.get("ok", True):
                return {"ok": False, "msg": f"Kotak order book not readable: {self.live.broker.get('msg')}"}
        self.mode = mode
        self.arm = {"armed": mode == "LIVE", "by": user, "at": dt.datetime.now(IST).strftime("%H:%M:%S")}
        try:
            self._arm_path().write_text(json.dumps(self.arm), encoding="utf-8")
        except Exception:
            log.exception("arm file write")
        log.warning("MODE -> %s by %s", mode, user)
        audit.write("mode", user, what=f"MODE -> {mode}")
        if self.rec:
            self.rec.event("mode", {"mode": mode, **self.arm})
        if self.loop:
            for fn in (self._broadcast_status, self._broadcast_book, self._broadcast_risk):
                self.loop.call_soon_threadsafe(fn)
        n = self.live.open_positions()
        return {"ok": True, "mode": mode,
                "msg": ("LIVE ARMED - orders now go to Kotak P" if mode == "LIVE"
                        else "PAPER - new orders are paper again" + (f" ({n} live position(s) still open)" if n else ""))}

    # ── startup ─────────────────────────────────────────────────────────────
    def start(self, loop):
        self.loop = loop
        try:
            s = self.sessions.get()
            self.universe = I.load(s.client)
        except Exception as e:
            self.error = f"startup: {e}"
            log.exception("startup failed")
            raise
        self.paper = PaperBook(self.universe, self.quote, on_change=self.on_book, settings=self.settings.get)
        self.live = LiveBook(self.universe, self.quote, self.sessions, armed=lambda: self.mode == "LIVE",
                             on_change=self.on_book, settings=self.settings.get)
        self.risks = {"PAPER": RiskGuard(self.paper, self.ltp, on_change=self.on_risk),
                      "LIVE": RiskGuard(self.live, self.ltp, on_change=self.on_risk,
                                        path=C.DATA / f"risk_live_{dt.datetime.now(IST).date().isoformat()}.json")}
        self._load_arm()
        self.timers = ExitTimers(self._timer_fire, on_change=self.on_timers)
        self.live.notify = self.on_note
        # per-position auto stop-loss (+ trail): real SL orders at Kotak in LIVE, watched in PAPER
        self.sls = {"PAPER": AutoSL(self.paper, self.ltp, self.settings.get, live=False,
                                    on_change=self.on_book, notify=self.on_note),
                    "LIVE": AutoSL(self.live, self.ltp, self.settings.get, live=True,
                                   on_change=self.on_book, notify=self.on_note)}
        for mode, bk in (("PAPER", self.paper), ("LIVE", self.live)):
            bk.before_reduce = self.sls[mode].before_reduce
            bk.before_flatten = self.sls[mode].before_flatten
        self.live.on_push_other = self.sls["LIVE"].on_push
        self.orderfeed = OrderFeed(self.sessions, self.live.on_push, self.live.on_push_position,
                                   on_reconnect=self.live._poll_kick.set).start()
        self.book_tokens = self.paper.tokens() | self.live.tokens() | self._away_tokens()
        self.rec = DayRecorder()
        if self.rec.active:                        # restarted mid-day: write a fresh header to today's file
            self.rec._activate_header = self._replay_header
        self.feed = KotakFeed(self.sessions, self.on_ticks, self.on_status)
        # always-on: every future + the nearest option expiry of each product
        first = []
        for p in self.universe.products.values():
            first += [f.token for f in p.futures[:3]]
            if p.chains:
                first += self.expiry_tokens(p.name, p.expiries()[0])
        self.feed.want(first)
        if self.book_tokens:
            self.feed.want_depth(list(self.book_tokens))
        self.feed.start()
        threading.Thread(target=self._housekeeping, daemon=True, name="Housekeeping").start()
        for s in self.sls.values():
            s.start()
        threading.Thread(target=self._margin_loop, daemon=True, name="Margin").start()

    def _margin_loop(self):
        """Kotak P margins for the right panel: READ-ONLY limits() call every 30 s on its own thread
        (never in the tick or order path)."""
        while True:
            self.read_margin()
            time.sleep(30)

    def read_margin(self) -> dict:
        """One READ-ONLY Kotak limits() call -> self.margin (also used by the screen's Refresh button)."""
        def num(d, *keys):
            for k in keys:
                if isinstance(d, dict) and d.get(k) not in (None, ""):
                    try:
                        return float(d[k])
                    except (TypeError, ValueError):
                        pass
            return None
        try:
            r = self.sessions.get().client.limits()
            if isinstance(r, dict) and not r.get("error"):
                self.margin = {
                    "ok": True,
                    "available": num(r, "Net", "net", "MarginAvailable", "AvailableMargin", "availableMargin"),
                    "used": num(r, "MarginUsed", "marginUsed", "MarginUtilized", "UtilizedMargin", "marginUtilized"),
                    "at": dt.datetime.now(IST).strftime("%H:%M:%S")}
            else:
                self.margin = {"ok": False, "msg": str(r)[:80]}
        except Exception as e:
            self.margin = {"ok": False, "msg": f"{type(e).__name__}"}
        return self.margin

    def refresh(self) -> dict:
        """Screen's Refresh button: re-read margin and Kotak's order book now, re-send everything."""
        m = self.read_margin()
        changed = self.live.poll() if self.live else False
        if self.live:
            self.live.sync_positions()
        if self.loop:
            for fn in (self._broadcast_status, self._broadcast_book, self._broadcast_risk):
                self.loop.call_soon_threadsafe(fn)
        ok = m.get("ok") and self.live.broker.get("ok", True)
        msg = "refreshed: margin" + (f" ₹{m['available']:,.0f} available" if m.get("available") is not None else "") + \
              (", Kotak orders read" + (" (updated)" if changed else "") if self.live.broker.get("ok", True)
               else f", Kotak orders NOT readable: {self.live.broker.get('msg')}")
        return {"ok": bool(ok), "msg": msg if m.get("ok") else f"margin NOT readable: {m.get('msg')}", "margin": m}

    def _housekeeping(self):
        """Every 0.5 s: re-price / push waiting paper orders. Every SAMPLE_S s: one MTM-curve point."""
        last_mtm = 0.0
        born = dt.datetime.now(IST)
        restart_at = next_restart(born)
        built_today = False
        build_missing_replays()                    # a day whose file was never built (engine was down at 23:58)
        while True:
            try:
                n = dt.datetime.now(IST)
                if not built_today and n.time() >= REPLAY_BUILD_AT and self.rec.active:
                    built_today = True
                    self.rec.flush()
                    spawn_replay_build(self.rec.day)
                if n >= restart_at:
                    # 00:05 = a new day (fresh books / guards / LIVE disarmed; positions re-read from Kotak);
                    # 08:45 = fresh Kotak login + contract list. The scheduled task starts the new engine.
                    log.warning("scheduled restart (%s): engine born %s - exiting", restart_at.strftime("%H:%M"), born)
                    audit.write("engine", what=f"scheduled restart {restart_at:%H:%M}")
                    logging.shutdown()
                    os._exit(3)
                if self.timers:
                    self.timers.tick()
                self.paper.housekeep()
                self.live.housekeep()
                now = time.time()
                if now - last_mtm >= SAMPLE_S:
                    last_mtm = now
                    for mode, bk in (("PAPER", self.paper), ("LIVE", self.live)):
                        h = self.mtms[mode]
                        pos = [p for p in bk.positions() if p["net"] or p["realized"]]
                        n_before = len(h.pts)
                        h.sample(pos, self.ltp, now)
                        if mode == "LIVE" and len(h.pts) > n_before:
                            self.rec.event("mtm", h.pts[-1])
                    self.atm_hist.sample(self.atm_straddles(), now)
            except Exception:
                log.exception("housekeeping")
            time.sleep(0.5)

    def expiry_tokens(self, name, expiry) -> list:
        ch = self.universe.products[name].chains.get(expiry, {})
        return [leg.token for legs in ch.values() for leg in legs.values()]

    def ltp(self, token):
        t = self.last.get(token)
        return t[1] if t else None

    def quote(self, token) -> dict:
        """Prices + book for the paper engine, quantities in UNITS. Kotak's MCX feed reports bid/ask
        and depth quantities in LOTS (seen 19-Oct: GOLDM depth 2,1,1,1,2 = lots of 100) -> x lot."""
        t = self.last.get(token)
        if not t:
            return {}
        c = self.universe.by_token.get(token)
        lot = c.lot if c else 1
        dp = self.depth.get(token)
        if dp:
            dp = ([(p, q * lot) for p, q in dp[0]], [(p, q * lot) for p, q in dp[1]])
        return {"ltp": t[1], "bid": t[2], "ask": t[3],
                "bq": t[4] * lot if t[4] is not None else None,
                "aq": t[5] * lot if t[5] is not None else None, "depth": dp}

    # ── feed thread -> loop ─────────────────────────────────────────────────
    def on_ticks(self, ticks):
        touched = []
        rec = self.rec
        rec_last = self._rec_last
        ms = now_ms()
        with self._plock:
            for t in ticks:
                tok = t[0]
                cur = self.last.get(tok)
                if cur is None:
                    cur = [tok, None, None, None, None, None, None, None, None, None, None, None]
                # merge: a lite tick has None for fields it does not carry
                for i in range(1, 12):
                    if t[i] is not None:
                        cur[i] = t[i]
                if t[12] is not None:
                    self.depth[tok] = t[12]
                self.last[tok] = cur
                self.pending[tok] = cur
                row = tuple(cur)                       # replay copy, only when something visible changed
                if rec_last.get(tok) != row:           # (a depth-only packet changes nothing on screen)
                    rec_last[tok] = row
                    rec.tick((ms, *row))
                if tok in self.book_tokens:
                    touched.append(tok)
            if not self.flush_scheduled and self.loop:
                self.flush_scheduled = True
                self.loop.call_soon_threadsafe(self._flush)
        for tok in touched:                        # resting paper orders vs the new book (live: Kotak fills)
            self.paper.on_tick(tok)
        if touched:                                # MTM guards (paper AND live) on every tick of a held contract
            for r in self.risks.values():
                r.check()

    def on_status(self, st):
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_status)

    def on_book(self):
        """Paper or live book changed (any thread)."""
        if self.paper is None or self.live is None:      # still starting up (the live book reads Kotak in its constructor)
            return
        self.book_tokens = self.paper.tokens() | self.live.tokens() | self._away_tokens()
        if self.feed and self.book_tokens:
            self.feed.want_depth(list(self.book_tokens))
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_book)
            self.loop.call_soon_threadsafe(self._broadcast_status)     # live open / working counts on the badge

    def _timer_fire(self, mode, name, frac) -> dict:
        """A timer's exit, on the book that was active when it was set."""
        book = self.live if mode == "LIVE" else self.paper
        return book.exit_instrument(name, frac)

    def on_note(self, ev: dict):
        """A live fill / reject that arrived after the order's pop-up: tell every screen."""
        if self.loop:
            msg = {"t": "note", **ev}
            self.loop.call_soon_threadsafe(lambda: [c.send_nowait(msg) for c in list(self.clients)])

    def on_timers(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_timers)

    def _broadcast_timers(self):
        msg = {"t": "timers", "items": self.timers.view() if self.timers else []}
        for c in list(self.clients):
            c.send_nowait(msg)

    def on_risk(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_risk)

    def _broadcast_risk(self):
        st = {"t": "risk", **self.risk.state()}
        self.rec.event("risk", {"t": "risk", **self.risks["LIVE"].state()})
        for c in list(self.clients):
            c.send_nowait(st)

    def atm_straddles(self) -> dict:
        """Per instrument, nearest option expiry: ATM strike (nearest to its future) and CE + PE LTP."""
        out = {}
        for name, p in self.universe.products.items():
            if not p.chains:
                continue
            exp = p.expiries()[0]
            und = p.underlying(exp)
            f = self.last.get(und.token) if und else None
            if not f or f[1] is None:
                continue
            ch = p.chains[exp]
            k = min(ch, key=lambda x: abs(x - f[1]))
            ce, pe = self.last.get(ch[k]["CE"].token), self.last.get(ch[k]["PE"].token)
            if ce and pe and ce[1] is not None and pe[1] is not None:
                out[name] = [k, round(ce[1] + pe[1], 2), exp]
        return out

    def _flush(self):
        now_s = time.time()
        if now_s - self._atm_at >= 1.0 and self.universe and self.clients:
            self._atm_at = now_s
            v = self.atm_straddles()
            if v:
                msg = {"t": "atm", "v": v}
                self.rec.event("atm", msg)
                for c in list(self.clients):
                    c.send_nowait(msg)
        with self._plock:
            batch = {k: list(v) for k, v in self.pending.items()}   # copies: the feed thread keeps mutating
            self.pending = {}
            self.flush_scheduled = False
        if not batch:
            return
        now = time.time()
        for c in list(self.clients):
            rows = [v for k, v in batch.items() if k in c.watch]
            if rows:
                c.send_nowait({"t": "d", "s": round(now, 3), "u": rows})

    def _broadcast_status(self):
        st = self.status()
        self.rec.event("status", self._rec_status())
        for c in list(self.clients):
            c.send_nowait({"t": "status", **st})

    def _broadcast_book(self):
        snap = self.book_msg()
        live = snap if self.mode == "LIVE" else self._rec_book()
        self.rec.event("book", live)
        if not self.rec.active and live["orders"]:     # first LIVE order of the day starts the replay file
            self.rec.activate(self._replay_header)
        for c in list(self.clients):
            c.refresh_watch(self.book_tokens)
            c.send_nowait(snap)

    def status(self) -> dict:
        f = self.feed.status() if self.feed else {}
        lv = self.live
        return {"feed": f, "error": self.error, "mode": self.mode, "uptime": round(time.time() - self.started_at),
                "arm": self.arm, "live_open": lv.open_positions() if lv else 0, "live_working": lv.working() if lv else 0,
                "broker": lv.broker if lv else {},
                "order_push": dict(self.orderfeed.status) if getattr(self, "orderfeed", None) else {},
                "pos_sync": {"ok": lv.positions_fresh(), "msg": lv.pos_sync.get("msg", ""),
                             "age": round(time.time() - lv.pos_sync["at"], 1) if lv.pos_sync.get("at") else None}
                if lv else {}}

    def _replay_header(self) -> dict:
        """Everything a replay needs at its start (called once by the recorder's writer thread)."""
        with self._plock:
            last = [list(v) for v in self.last.values()]
        return {"meta": self.meta(), "chains": list(self.chain_defs.values()), "last": last,
                "book": self._rec_book(), "risk": {"t": "risk", **self.risks["LIVE"].state()},
                "status": self._rec_status(), "settings": self.settings.get(),
                "mtm": list(self.mtms["LIVE"].pts)}

    # ── screen requests ─────────────────────────────────────────────────────
    def meta(self) -> dict:
        u = self.universe
        prods = []
        for n in C.INSTRUMENTS:
            p = u.products[n]
            futs = [{"token": f.token, "symbol": f.symbol, "expiry": f.expiry} for f in p.futures[:3]]
            prods.append({"name": n, "label": C.LABELS[n], "lot": p.lot, "tick": p.opt_tick,
                          "expiries": p.expiries(), "futures": futs})
        return {"t": "meta", "products": prods, "strikes": list(C.STRIKE_CHOICES),
                "default_strikes": C.DEFAULT_STRIKES, "file_date": u.file_date, "mode": self.mode,
                "max_lots": C.MAX_LOTS_PER_ORDER, "confirm_lots": C.CONFIRM_LOTS,
                "qty_lots_max": C.MAX_QTY_LOTS_CHOICE, "buffer_pct": C.MKT_BUFFER_PCT,
                "settings": self.settings.get(), "gap_choices": GAP_CHOICES}

    def chain(self, name, expiry) -> dict:
        p = self.universe.products[name]
        if expiry not in p.chains:
            expiry = p.expiries()[0]
        toks = self.expiry_tokens(name, expiry)
        self.feed.want_depth(toks)                 # 5-level book for paper fills on this chain
        und = p.underlying(expiry)
        rows = [{"k": k, "ce": legs["CE"].token, "pe": legs["PE"].token,
                 "cs": legs["CE"].symbol, "ps": legs["PE"].symbol}
                for k, legs in p.chains[expiry].items()]
        snap = [self.last[t] for t in toks + ([und.token] if und else []) if t in self.last]
        d = {"t": "chain", "name": name, "expiry": expiry, "lot": p.lot, "tick": p.opt_tick,
             "fut": {"token": und.token, "symbol": und.symbol} if und else None, "rows": rows}
        if (name, expiry) not in self.chain_defs:
            self.chain_defs[(name, expiry)] = d
            self.rec.event("chain", d)
        return {**d, "snap": snap}


class Client:
    def __init__(self, ws: WebSocket, loop):
        self.ws = ws
        self.loop = loop
        self.base: set[int] = set()                # chain + futures
        self.watch: set[int] = set()               # base + open positions / orders
        self.q: asyncio.Queue = asyncio.Queue(maxsize=2000)

    def refresh_watch(self, book_tokens):
        self.watch = self.base | book_tokens

    def send_nowait(self, obj):
        try:
            self.q.put_nowait(obj)
        except asyncio.QueueFull:
            pass                                   # a stalled screen must never slow the engine

    async def writer(self):
        while True:
            obj = await self.q.get()
            await self.ws.send_text(json.dumps(obj, separators=(",", ":")))


ENGINE = Engine()


def spawn_replay_build(day: str):
    """Build MCX_<day>.html in a separate process (CPU-heavy; the engine process is never involved)."""
    log.info("building replay file for %s (separate process)", day)
    subprocess.Popen([sys.executable, "-m", "live_trading_options.mcx_terminal.replay_build", day],
                     cwd=str(C.ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def build_missing_replays():
    today = dt.datetime.now(IST).date().isoformat()
    for raw in sorted(REPLAY_DIR.glob("raw_*.mcxr")):
        day = raw.stem[4:]
        if day < today and not (REPLAY_DIR / f"MCX_{day}.html").exists():
            spawn_replay_build(day)


def _order(m):
    c = ENGINE.universe.by_token.get(int(m["token"]))
    if c is None:
        return {"ok": False, "msg": "unknown contract"}
    lots = int(m.get("lots", 0))
    lim = m.get("limit")
    kind = "LMT" if lim not in (None, "", 0) else "MKT"
    return ENGINE.book.place(c.token, m["side"], lots * c.lot, kind, float(lim) if kind == "LMT" else None,
                             source=m.get("source", ""))


def _basket(m):
    legs = []
    for l in m.get("legs", []):
        c = ENGINE.universe.by_token.get(int(l["token"]))
        if c is None:
            return {"ok": False, "msg": "unknown contract in basket"}
        legs.append({"token": c.token, "side": l["side"], "qty": int(l["lots"]) * c.lot})
    if not legs:
        return {"ok": False, "msg": "empty basket"}
    return ENGINE.book.basket(legs, source=m.get("source", "basket"))


def _settings_set(m):
    r = ENGINE.settings.update(m.get("values") or {})
    ENGINE.rec.event("settings", ENGINE.settings.get())
    return {**r, "settings": ENGINE.settings.get(), "tag": m.get("tag")}


def _sl_apply(m):
    """APPLY: save the row's values, then place / move every stop to them."""
    if m.get("values"):
        r = ENGINE.settings.update({"auto_sl": m["values"]})
        if not r["ok"]:
            return r
    return {**ENGINE.sl.apply(), "settings": ENGINE.settings.get()}


ORDER_OPS = {
    "order": _order,
    "basket": _basket,
    "move": lambda m: ENGINE.book.move(int(m["token"]), int(m.get("steps", 1)), bool(m.get("away", True)),
                                       int(m["lots"]) if m.get("lots") else None),
    "sl_set": lambda m: ENGINE.sl.set_one(int(m["token"]), bool(m.get("on", True)), m.get("trigger"), m.get("qty")),
    "risk_set": lambda m: ENGINE.risk.set(m.get("stop"), m.get("target"), m.get("trail")),
    "risk_clear": lambda m: ENGINE.risk.clear(),
    "sl_drop": lambda m: ENGINE.sl.drop(int(m["token"])),
    "sl_apply": lambda m: _sl_apply(m),
    "settings_set": lambda m: _settings_set(m),
    "margin": lambda m: {"quiet": True, **ENGINE.margin},
    "refresh": lambda m: ENGINE.refresh(),
    "mode_set": lambda m: ENGINE.set_mode(m.get("mode", ""), m.get("_user", ""), m.get("confirm", "")),
    "atm_hist": lambda m: {"ok": True, "quiet": True, **ENGINE.atm_hist.query(m.get("name", ""), m.get("mins", 1))},
    "mtm_hist": lambda m: {"ok": True, "quiet": True, "tag": m.get("tag"),
                           **ENGINE.mtm.query(m.get("mins", 1), m.get("inst", "all"), m.get("kind", "pnl"))},
    "cancel": lambda m: ENGINE.book.cancel(int(m["id"])),
    "cancel_all": lambda m: {"ok": True, "msg": f"cancelled {ENGINE.book.cancel_all()} order(s)"},
    "exit": lambda m: ENGINE.book.exit_position(int(m["token"]), float(m.get("frac", 1.0))),
    "exit_all": lambda m: ENGINE.book.exit_all(float(m.get("frac", 1.0))),
    "exit_inst": lambda m: ENGINE.book.exit_instrument(str(m.get("name", "")), float(m.get("frac", 1.0))),
    "timer_set": lambda m: ENGINE.timers.set(str(m.get("name", "")), m.get("frac", 1.0), m.get("at", ""),
                                             ENGINE.mode, m.get("_user", "")),
    "timer_clear": lambda m: ENGINE.timers.clear(str(m.get("name", "")), m.get("frac", 1.0), m.get("_user", "")),
}

app = FastAPI(title="MCX Terminal")
app.add_middleware(AccessGuard)               # second lock behind Cloudflare Access
app.mount("/static", StaticFiles(directory=C.STATIC), name="static")


@app.on_event("startup")
async def _startup():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ENGINE.start, loop)


@app.get("/")
async def index():
    return FileResponse(C.STATIC / "index.html", headers={"Cache-Control": "no-cache"})


DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/audit")
async def audit_days():
    return JSONResponse(audit.days())


@app.get("/audit/{day}")
async def audit_day(day: str):
    if not DAY_RE.match(day):
        return JSONResponse({"error": "bad day"}, status_code=400)
    return JSONResponse(audit.read(day))


@app.get("/replays")
async def replays():
    """Recorded days, newest first (raw recording + whether the double-click file is built)."""
    out = []
    for raw in sorted(REPLAY_DIR.glob("raw_*.mcxr"), reverse=True):
        day = raw.stem[4:]
        html = REPLAY_DIR / f"MCX_{day}.html"
        out.append({"day": day, "raw_mb": round(raw.stat().st_size / 1e6, 1),
                    "html_mb": round(html.stat().st_size / 1e6, 1) if html.exists() else None,
                    "today": day == ENGINE.rec.day if ENGINE.rec else False})
    return JSONResponse(out)


@app.get("/replays/MCX_{day}.html")
async def replay_file(day: str):
    """Download a day's replay file; (re)built first if the recording is newer (e.g. today, mid-session)."""
    if not DAY_RE.match(day):
        return JSONResponse({"error": "bad day"}, status_code=400)
    raw, html = REPLAY_DIR / f"raw_{day}.mcxr", REPLAY_DIR / f"MCX_{day}.html"
    if not raw.exists():
        return JSONResponse({"error": "no recording for that day"}, status_code=404)
    if ENGINE.rec and day == ENGINE.rec.day:
        await asyncio.get_running_loop().run_in_executor(None, ENGINE.rec.flush)
    if not html.exists() or html.stat().st_mtime < raw.stat().st_mtime - 5:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "live_trading_options.mcx_terminal.replay_build", day, cwd=str(C.ROOT),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _, err = await proc.communicate()
        if proc.returncode != 0 or not html.exists():
            return JSONResponse({"error": "build failed", "detail": err.decode(errors="replace")[-400:]}, status_code=500)
    return FileResponse(html, media_type="text/html", filename=html.name)


@app.get("/health")
async def health():
    return JSONResponse(ENGINE.status())


QUIET_OPS = {"margin", "mtm_hist", "atm_hist"}
SLOW_OPS = {"exit_all", "exit", "exit_inst", "refresh", "cancel_all"}


async def run_op(c, loop, op, msg, user):
    try:
        res = await loop.run_in_executor(None, ORDER_OPS[op], msg)
    except Exception as e:
        log.exception("order op %s", op)
        res = {"ok": False, "msg": _txt(e)}
    if op not in QUIET_OPS:
        args = {k: v for k, v in msg.items() if k not in ("op", "req", "_user", "confirm")}
        audit.write("op", user, op=op, mode=ENGINE.mode, args=args, ok=res.get("ok"),
                    result=str(res.get("msg", ""))[:300])
    c.send_nowait({"t": "ack", "op": op, "req": msg.get("req"), **res})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    c = Client(ws, loop)
    user = ws.scope.get("mcx_user", "local")       # set by AccessGuard (verified Cloudflare email)
    ENGINE.clients.add(c)
    wtask = asyncio.create_task(c.writer())
    try:
        c.send_nowait(ENGINE.meta())
        c.send_nowait({"t": "status", **ENGINE.status()})
        c.send_nowait(ENGINE.book_msg())
        c.send_nowait({"t": "risk", **ENGINE.risk.state()})
        c.send_nowait({"t": "timers", "items": ENGINE.timers.view() if ENGINE.timers else []})
        c.send_nowait({"t": "atm", "v": ENGINE.atm_straddles()})
        all_futs = {f.token for p in ENGINE.universe.products.values() for f in p.futures[:3]}
        while True:
            msg = json.loads(await ws.receive_text())
            op = msg.get("op")
            if op == "ping":
                c.send_nowait({"t": "pong", "c": msg.get("c"), "s": round(time.time(), 3)})
            elif op == "chain":
                ch = await loop.run_in_executor(None, ENGINE.chain, msg["name"], msg.get("expiry"))
                c.base = {r["ce"] for r in ch["rows"]} | {r["pe"] for r in ch["rows"]} | all_futs
                c.refresh_watch(ENGINE.book_tokens)
                c.send_nowait(ch)
                c.send_nowait({"t": "d", "s": round(time.time(), 3),
                               "u": [ENGINE.last[t] for t in c.watch if t in ENGINE.last]})
                c.send_nowait({"t": "status", **ENGINE.status()})       # subscription counts changed
            elif op == "ui":                           # a button press on the screen (audit only)
                audit.write("ui", user, what=str(msg.get("what", ""))[:120], mode=ENGINE.mode)
            elif op in ORDER_OPS:
                msg["_user"] = user                        # who is asking (never taken from the screen)
                if op in SLOW_OPS:                         # exits can take seconds: never hold this screen's socket
                    asyncio.create_task(run_op(c, loop, op, msg, user))
                else:
                    await run_op(c, loop, op, msg, user)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("ws handler")
    finally:
        ENGINE.clients.discard(c)
        wtask.cancel()


def main():
    _setup_logging()
    sys.setswitchinterval(0.0005)   # background threads hand the CPU back to the feed / orders within 0.5 ms
    log.info("MCX terminal starting on %s:%s (stage 5: paper + owner-armed live Kotak P orders)", C.HOST, C.PORT)
    audit.write("engine", what="terminal started")
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
