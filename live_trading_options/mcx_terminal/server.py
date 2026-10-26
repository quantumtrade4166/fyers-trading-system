"""
mcx_terminal/server.py — the terminal's engine + web server (Kotak P, MCX options).
==================================================================================

Own process, own port (47680), own Kotak session. Never touches the dashboard or any other engine.
Stage 2 = PAPER orders (paper.py). No code in this process can send an order to Kotak.

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

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.access import AccessGuard
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal.feed import KotakFeed
from live_trading_options.mcx_terminal.kotak_session import SessionHolder
from live_trading_options.mcx_terminal.paper import PaperBook
from live_trading_options.mcx_terminal.risk import RiskGuard
from live_trading_options.mcx_terminal.settings import Settings
from live_trading_options.mcx_terminal.mtm import AtmHistory, MtmHistory, SAMPLE_S
from live_trading_options.mcx_terminal.recorder import DayRecorder, now_ms, REPLAY_DIR

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
log = logging.getLogger("mcx.server")
DAILY_RESTART = dt.time(8, 45)    # fresh Kotak login + contract list before MCX opens at 09:00
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
        self.book: PaperBook | None = None
        self.risk: RiskGuard | None = None
        self.settings = Settings()
        self.mtm = MtmHistory()
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
        self.book = PaperBook(self.universe, self.quote, on_change=self.on_book, settings=self.settings.get)
        self.book_tokens = self.book.tokens()
        self.risk = RiskGuard(self.book, self.ltp, on_change=self.on_risk)
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
        threading.Thread(target=self._margin_loop, daemon=True, name="Margin").start()

    def _margin_loop(self):
        """Kotak P margins for the right panel: READ-ONLY limits() call every 30 s on its own thread
        (never in the tick or order path). Field names are logged once so they can be checked."""
        logged = False

        def num(d, *keys):
            for k in keys:
                if isinstance(d, dict) and d.get(k) not in (None, ""):
                    try:
                        return float(d[k])
                    except (TypeError, ValueError):
                        pass
            return None
        while True:
            try:
                r = self.sessions.get().client.limits()
                if isinstance(r, dict) and not r.get("error"):
                    if not logged:
                        log.info("Kotak limits() fields: %s", sorted(r.keys()))
                        logged = True
                    self.margin = {
                        "ok": True,
                        "available": num(r, "Net", "net", "MarginAvailable", "AvailableMargin", "availableMargin"),
                        "used": num(r, "MarginUsed", "marginUsed", "MarginUtilized", "UtilizedMargin", "marginUtilized"),
                        "at": dt.datetime.now(IST).strftime("%H:%M:%S")}
                else:
                    self.margin = {"ok": False, "msg": str(r)[:80]}
            except Exception as e:
                self.margin = {"ok": False, "msg": f"{type(e).__name__}"}
            time.sleep(30)

    def _housekeeping(self):
        """Every 0.5 s: re-price / push waiting paper orders. Every SAMPLE_S s: one MTM-curve point."""
        last_mtm = 0.0
        born = dt.datetime.now(IST).date()
        built_today = False
        build_missing_replays()                    # a day whose file was never built (engine was down at 23:58)
        while True:
            try:
                n = dt.datetime.now(IST)
                if not built_today and n.time() >= REPLAY_BUILD_AT and self.rec.active:
                    built_today = True
                    self.rec.flush()
                    spawn_replay_build(self.rec.day)
                if n.date() > born and n.time() >= DAILY_RESTART:
                    # a new trading day: exit so the scheduled task starts a fresh engine (new Kotak
                    # session, today's contract list, today's paper book with positions carried over)
                    log.warning("daily restart: engine born %s, now %s - exiting for a fresh start", born, n)
                    logging.shutdown()
                    os._exit(3)
                self.book.housekeep()
                now = time.time()
                if now - last_mtm >= SAMPLE_S:
                    last_mtm = now
                    pos = [p for p in self.book.positions() if p["net"] or p["realized"]]
                    n_before = len(self.mtm.pts)
                    self.mtm.sample(pos, self.ltp, now)
                    if len(self.mtm.pts) > n_before:
                        self.rec.event("mtm", self.mtm.pts[-1])
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
        for tok in touched:                        # resting paper orders vs the new book
            self.book.on_tick(tok)
        if touched and self.risk:                  # MTM guard on every tick of a held contract
            self.risk.check()

    def on_status(self, st):
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_status)

    def on_book(self):
        """Paper book changed (any thread)."""
        self.book_tokens = self.book.tokens()
        if self.feed and self.book_tokens:
            self.feed.want_depth(list(self.book_tokens))
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_book)

    def on_risk(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self._broadcast_risk)

    def _broadcast_risk(self):
        st = {"t": "risk", **self.risk.state()}
        self.rec.event("risk", st)
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
        self.rec.event("status", {"t": "status", **st})
        for c in list(self.clients):
            c.send_nowait({"t": "status", **st})

    def _broadcast_book(self):
        snap = {"t": "book", **self.book.snapshot()}
        self.rec.event("book", snap)
        if not self.rec.active and snap["orders"]:     # first order of the day starts the replay file
            self.rec.activate(self._replay_header)
        for c in list(self.clients):
            c.refresh_watch(self.book_tokens)
            c.send_nowait(snap)

    def status(self) -> dict:
        f = self.feed.status() if self.feed else {}
        return {"feed": f, "error": self.error, "mode": C.MODE, "uptime": round(time.time() - self.started_at)}

    def _replay_header(self) -> dict:
        """Everything a replay needs at its start (called once by the recorder's writer thread)."""
        with self._plock:
            last = [list(v) for v in self.last.values()]
        return {"meta": self.meta(), "chains": list(self.chain_defs.values()), "last": last,
                "book": {"t": "book", **self.book.snapshot()}, "risk": {"t": "risk", **self.risk.state()},
                "status": {"t": "status", **self.status()}, "settings": self.settings.get(),
                "mtm": list(self.mtm.pts)}

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
                "default_strikes": C.DEFAULT_STRIKES, "file_date": u.file_date, "mode": C.MODE,
                "max_lots": C.MAX_LOTS_PER_ORDER, "confirm_lots": C.CONFIRM_LOTS,
                "qty_lots_max": C.MAX_QTY_LOTS_CHOICE, "buffer_pct": C.MKT_BUFFER_PCT,
                "settings": self.settings.get()}

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
    return {**r, "settings": ENGINE.settings.get()}


ORDER_OPS = {
    "order": _order,
    "basket": _basket,
    "move": lambda m: ENGINE.book.move(int(m["token"]), int(m.get("steps", 1)), bool(m.get("away", True))),
    "risk_set": lambda m: ENGINE.risk.set(m.get("stop"), m.get("target"), m.get("trail")),
    "risk_clear": lambda m: ENGINE.risk.clear(),
    "settings_set": lambda m: _settings_set(m),
    "margin": lambda m: {"quiet": True, **ENGINE.margin},
    "atm_hist": lambda m: {"ok": True, "quiet": True, **ENGINE.atm_hist.query(m.get("name", ""), m.get("mins", 1))},
    "mtm_hist": lambda m: {"ok": True, "quiet": True, "tag": m.get("tag"),
                           **ENGINE.mtm.query(m.get("mins", 1), m.get("inst", "all"), m.get("kind", "pnl"))},
    "cancel": lambda m: ENGINE.book.cancel(int(m["id"])),
    "cancel_all": lambda m: {"ok": True, "msg": f"cancelled {ENGINE.book.cancel_all()} order(s)"},
    "exit": lambda m: ENGINE.book.exit_position(int(m["token"]), float(m.get("frac", 1.0))),
    "exit_all": lambda m: ENGINE.book.exit_all(float(m.get("frac", 1.0))),
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


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    c = Client(ws, loop)
    ENGINE.clients.add(c)
    wtask = asyncio.create_task(c.writer())
    try:
        c.send_nowait(ENGINE.meta())
        c.send_nowait({"t": "status", **ENGINE.status()})
        c.send_nowait({"t": "book", **ENGINE.book.snapshot()})
        c.send_nowait({"t": "risk", **ENGINE.risk.state()})
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
            elif op in ORDER_OPS:
                try:
                    res = await loop.run_in_executor(None, ORDER_OPS[op], msg)
                except Exception as e:
                    log.exception("order op %s", op)
                    res = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
                c.send_nowait({"t": "ack", "op": op, "req": msg.get("req"), **res})
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
    log.info("MCX terminal starting on %s:%s (%s, stage 3: paper orders + risk guard)", C.HOST, C.PORT, C.MODE)
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
