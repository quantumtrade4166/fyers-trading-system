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
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal.feed import KotakFeed
from live_trading_options.mcx_terminal.kotak_session import SessionHolder
from live_trading_options.mcx_terminal.paper import PaperBook

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
log = logging.getLogger("mcx.server")


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
        self.book_tokens: set[int] = set()         # tokens with an open position / open order
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
        self.book = PaperBook(self.universe, self.quote, on_change=self.on_book)
        self.book_tokens = self.book.tokens()
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

    def expiry_tokens(self, name, expiry) -> list:
        ch = self.universe.products[name].chains.get(expiry, {})
        return [leg.token for legs in ch.values() for leg in legs.values()]

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
                if tok in self.book_tokens:
                    touched.append(tok)
            if not self.flush_scheduled and self.loop:
                self.flush_scheduled = True
                self.loop.call_soon_threadsafe(self._flush)
        for tok in touched:                        # resting paper orders vs the new book
            self.book.on_tick(tok)

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

    def _flush(self):
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
        for c in list(self.clients):
            c.send_nowait({"t": "status", **st})

    def _broadcast_book(self):
        snap = {"t": "book", **self.book.snapshot()}
        for c in list(self.clients):
            c.refresh_watch(self.book_tokens)
            c.send_nowait(snap)

    def status(self) -> dict:
        f = self.feed.status() if self.feed else {}
        return {"feed": f, "error": self.error, "mode": C.MODE, "uptime": round(time.time() - self.started_at)}

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
                "qty_lots_max": C.MAX_QTY_LOTS_CHOICE, "buffer_pct": C.MKT_BUFFER_PCT}

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
        return {"t": "chain", "name": name, "expiry": expiry, "lot": p.lot, "tick": p.opt_tick,
                "fut": {"token": und.token, "symbol": und.symbol} if und else None,
                "rows": rows, "snap": snap}


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


def _order(m):
    c = ENGINE.universe.by_token.get(int(m["token"]))
    if c is None:
        return {"ok": False, "msg": "unknown contract"}
    lots = int(m.get("lots", 0))
    lim = m.get("limit")
    kind = "LMT" if lim not in (None, "", 0) else "MKT"
    return ENGINE.book.place(c.token, m["side"], lots * c.lot, kind, float(lim) if kind == "LMT" else None,
                             source=m.get("source", ""))


ORDER_OPS = {
    "order": _order,
    "cancel": lambda m: ENGINE.book.cancel(int(m["id"])),
    "cancel_all": lambda m: {"ok": True, "msg": f"cancelled {ENGINE.book.cancel_all()} order(s)"},
    "exit": lambda m: ENGINE.book.exit_position(int(m["token"]), float(m.get("frac", 1.0))),
    "exit_all": lambda m: ENGINE.book.exit_all(float(m.get("frac", 1.0))),
}

app = FastAPI(title="MCX Terminal")
app.mount("/static", StaticFiles(directory=C.STATIC), name="static")


@app.on_event("startup")
async def _startup():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, ENGINE.start, loop)


@app.get("/")
async def index():
    return FileResponse(C.STATIC / "index.html", headers={"Cache-Control": "no-cache"})


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
    log.info("MCX terminal starting on %s:%s (%s, stage 2: paper orders)", C.HOST, C.PORT, C.MODE)
    uvicorn.run(app, host=C.HOST, port=C.PORT, log_level="warning", ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
