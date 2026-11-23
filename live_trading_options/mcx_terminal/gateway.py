"""
mcx_terminal/gateway.py - the ORDER GATEWAY: the desk's LIVE order path in its own small process.
====================================================================================================
See GATEWAY_DESIGN.md. Holds the LIVE book (Kotak P orders, fills, positions), the LIVE auto stop-loss,
the LIVE MTM guard (stop / target / trail), the LIVE exit timers and the LIVE arm switch - and nothing else.
The screen process (server.py) talks to it on 127.0.0.1; if the screen freezes or restarts, the stops,
the guard and the timers keep running here, and the emergency page still exits.

Run:   set TERMINAL_DESK=MCX & python -m live_trading_options.mcx_terminal.gateway      (NSE: TERMINAL_DESK=NSE)
Port:  the desk's port + 1 (MCX 47681, NSE 47683), 127.0.0.1 only. Every call carries the secret from
       data*/gateway_secret.txt (written on first start, readable on the VPS only).
"""
import asyncio
import datetime as dt
import json
import logging
import os
import secrets
import threading
import time

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.gateway")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

PORT = C.PORT + 1
QUOTE_FRESH_S = 2.0           # own feed quote younger than this is used as is
HINT_FRESH_S = 1.0            # the screen's quote sent with a click is used if younger than this
PUSH_MIN_S = 0.15             # at most ~7 pushes a second per kind to the screen
SECRET_HEADER = "x-gateway-secret"
OPS_NEED_ARM = {"order", "basket", "move"}          # new risk; exits / cancels always allowed


def next_restart(born: dt.datetime) -> dt.datetime:
    """The first daily reset (C.RESET_AT) or refresh (C.REFRESH_AT) after `born` - as server.py does."""
    d = born.date()
    cands = [dt.datetime.combine(d + dt.timedelta(days=k), t, tzinfo=IST)
             for k in (0, 1) for t in (C.RESET_AT, C.REFRESH_AT)]
    return min(x for x in cands if x > born)


def secret_path():
    return C.DATA / "gateway_secret.txt"


def load_secret(create: bool = False) -> str:
    p = secret_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    if not create:
        return ""
    s = secrets.token_hex(24)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(s, encoding="utf-8")
    return s


class LiveSettings:
    """settings.json is written by the screen; the gateway re-reads it when the file changes."""

    def __init__(self, path=None):
        from live_trading_options.mcx_terminal.settings import Settings
        self._cls = Settings
        self.path = path or (C.DATA / "settings.json")
        self._s = Settings(self.path)
        self._mtime = self._stat()
        self._lock = threading.Lock()

    def _stat(self):
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def get(self) -> dict:
        m = self._stat()
        if m != self._mtime:
            with self._lock:
                if m != self._mtime:
                    try:
                        self._s = self._cls(self.path)
                    except Exception:
                        log.exception("settings reload")
                    self._mtime = m
        return self._s.get()


class Gateway:
    """The LIVE order path. Built from parts that already exist (LiveBook, AutoSL, RiskGuard, ExitTimers);
    `client` / `universe` / `start=False` are test hooks."""

    def __init__(self, sessions=None, universe=None, client=None, start=True, settings=None):
        from live_trading_options.mcx_terminal.live import LiveBook
        from live_trading_options.mcx_terminal.risk import RiskGuard
        from live_trading_options.mcx_terminal.sched import ExitTimers
        from live_trading_options.mcx_terminal.sl import AutoSL
        self.sessions = sessions
        self.loop = None
        self.listeners: set = set()               # asyncio queues of /stream subscribers
        self._push_at: dict = {}
        self._push_pending: set = set()
        self._plock = threading.Lock()
        self.last: dict = {}                      # token -> [tok, ltp, bid, ask, bq, aq, ...] as the screen keeps it
        self.last_at: dict = {}                   # token -> when our feed last priced it
        self.depth: dict = {}
        self.hints: dict = {}                     # token -> (time, quote dict) sent by the screen with a click
        self.watch: set = set()                   # contracts the screen shows (priced by our feed too)
        self.feed = None
        self.orderfeed = None
        self.settings = settings or LiveSettings()
        self.arm = {"armed": False}
        self._load_arm()
        if universe is None:
            from live_trading_options.mcx_terminal import instruments as I
            universe = I.load(sessions.get().client)
        self.universe = universe
        self.live = LiveBook(universe, self.quote, sessions, armed=lambda: bool(self.arm.get("armed")),
                             on_change=lambda: self.on_change("book"), settings=self.settings.get,
                             client=client, start_poller=start)
        self.risk = RiskGuard(self.live, self.ltp, on_change=lambda: self.on_change("risk"),
                              path=C.DATA / f"risk_live_{C.day()}.json")
        self.timers = ExitTimers(self._timer_fire, on_change=lambda: self.on_change("timers"),
                                 path=C.DATA / f"sched_exits_live_{C.day()}.json")
        self.sl = AutoSL(self.live, self.ltp, self.settings.get, live=True,
                         on_change=lambda: self.on_change("book"), notify=self.on_note, client=client)
        self.live.notify = self.on_note
        self.live.before_reduce = self.sl.before_reduce
        self.live.before_flatten = self.sl.before_flatten
        self.live.on_push_other = self.sl.on_push
        self.live.sl_ids = self.sl.working_ids
        self.started_at = time.time()
        if start:
            self._start_threads()

    # ── startup ─────────────────────────────────────────────────────────────
    def _start_threads(self):
        from live_trading_options.mcx_terminal.feed import KotakFeed
        from live_trading_options.mcx_terminal.orderfeed import OrderFeed
        self.orderfeed = OrderFeed(self.sessions, self.live.on_push, self.live.on_push_position,
                                   on_reconnect=self.live._poll_kick.set).start()
        self.feed = KotakFeed(self.sessions, self.on_ticks, seg_of=self.seg_of)
        self._resubscribe()
        self.feed.start()
        self.sl.start()
        threading.Thread(target=self._housekeeping, daemon=True, name="GwHousekeeping").start()

    def seg_of(self, token) -> str:
        c = self.universe.by_token.get(token)
        return c.seg if c else "mcx_fo"

    def _held_tokens(self) -> set:
        return set(self.live.tokens()) | {int(t) for t in (self.sl.view() or {})}

    def _resubscribe(self):
        if self.feed:
            toks = sorted(self._held_tokens() | self.watch)
            if toks:
                self.feed.want_depth(toks)

    def _housekeeping(self):
        born = dt.datetime.now(IST)
        restart_at = next_restart(born)
        while True:
            try:
                self.timers.tick()
                self.live.housekeep()
                if dt.datetime.now(IST) >= restart_at:
                    # same moments as the screen: 00:05 new day (fresh files, LIVE disarmed), 08:45 fresh Kotak
                    # login + contract list. The launcher ends on code 3; the scheduled task starts a new one.
                    log.warning("scheduled restart (%s): gateway born %s - exiting", restart_at.strftime("%H:%M"), born)
                    audit.write("engine", what=f"order gateway scheduled restart {restart_at:%H:%M}")
                    logging.shutdown()
                    os._exit(3)
            except Exception:
                log.exception("gateway housekeeping")
            time.sleep(0.5)

    # ── arm (the gateway owns it; the screen forwards the owner's verified request) ─────────────────
    def _arm_path(self):
        return C.DATA / f"live_arm_{C.day()}.json"

    def _load_arm(self):
        try:
            p = self._arm_path()
            if p.exists():
                a = json.loads(p.read_text(encoding="utf-8"))
                if a.get("armed"):
                    self.arm = a
                    log.warning("LIVE re-armed from today's arm file (by %s at %s)", a.get("by"), a.get("at"))
        except Exception:
            log.exception("arm file")

    def set_mode(self, mode: str, user: str) -> dict:
        """Called only by the screen AFTER it checked the owner and the typed word LIVE."""
        mode = str(mode).upper()
        if mode not in ("PAPER", "LIVE"):
            return {"ok": False, "msg": "mode must be PAPER or LIVE"}
        if mode == "LIVE" and not self.live.broker.get("ok", True):
            return {"ok": False, "msg": f"Kotak order book not readable: {self.live.broker.get('msg')}"}
        self.arm = {"armed": mode == "LIVE", "by": user, "at": dt.datetime.now(IST).strftime("%H:%M:%S")}
        try:
            self._arm_path().write_text(json.dumps(self.arm), encoding="utf-8")
        except Exception:
            log.exception("arm file write")
        log.warning("GATEWAY MODE -> %s by %s", mode, user)
        audit.write("mode", user, what=f"MODE -> {mode} (gateway)")
        self.on_change("status")
        n = self.live.open_positions()
        return {"ok": True, "mode": mode,
                "msg": ("LIVE ARMED - orders now go to Kotak P" if mode == "LIVE"
                        else "PAPER - new orders are paper again" + (f" ({n} live position(s) still open)" if n else ""))}

    # ── prices ──────────────────────────────────────────────────────────────
    def on_ticks(self, ticks):
        touched = False
        now = time.time()
        held = self._held_tokens()
        with self._plock:
            for t in ticks:
                tok = t[0]
                cur = self.last.get(tok) or [tok] + [None] * 11
                for i in range(1, 12):
                    if t[i] is not None:
                        cur[i] = t[i]
                if len(t) > 12 and t[12] is not None:
                    self.depth[tok] = t[12]
                self.last[tok] = cur
                self.last_at[tok] = now
                touched |= tok in held
        if touched:
            self.risk.check()
            try:
                self.live.on_prices({t[0] for t in ticks})   # a working exit the market ran past: re-price now
            except Exception:
                log.exception("exit chase")

    def ltp(self, token):
        t = self.last.get(token)
        return t[1] if t else None

    def quote(self, token) -> dict:
        """Own feed (fresh) -> the screen's hint (fresh) -> {} (the book then prices exits off REST)."""
        now = time.time()
        t = self.last.get(token)
        at = self.last_at.get(token, 0)
        if t and now - at < QUOTE_FRESH_S:
            c = self.universe.by_token.get(token)
            lot = c.lot if c and c.seg == "mcx_fo" else 1
            dp = self.depth.get(token)
            if dp:
                dp = ([(p, q * lot) for p, q in dp[0]], [(p, q * lot) for p, q in dp[1]])
            return {"ltp": t[1], "bid": t[2], "ask": t[3],
                    "bq": t[4] * lot if t[4] is not None else None,
                    "aq": t[5] * lot if t[5] is not None else None, "depth": dp}
        h = self.hints.get(token)
        if h and now - h[0] < HINT_FRESH_S:
            return dict(h[1])
        if t:                                     # stale own price: still better than nothing for ltp-based logic
            return {"ltp": t[1], "bid": None, "ask": None, "bq": None, "aq": None, "depth": None}
        return {}

    def hint(self, token, q: dict):
        """The screen's quote for a contract it is about to trade, and it asks us to stream it too."""
        if q:
            self.hints[int(token)] = (time.time(), {k: q.get(k) for k in ("ltp", "bid", "ask", "bq", "aq", "depth")})
        if int(token) not in self.watch:
            self.watch.add(int(token))
            self._resubscribe()

    def set_watch(self, tokens):
        new = {int(t) for t in tokens or []}
        if new != self.watch:
            self.watch = new
            self._resubscribe()

    # ── ops (same argument shapes as the screen's ORDER_OPS) ─────────────────
    def _timer_fire(self, mode, name, frac) -> dict:
        return self.live.exit_instrument(name, frac)

    def op(self, name: str, m: dict) -> dict:
        if name in OPS_NEED_ARM and not self.arm.get("armed"):
            return {"ok": False, "msg": "LIVE is not armed on the order gateway - arm LIVE first"}
        if m.get("hint") and m.get("token") is not None:
            self.hint(m["token"], m["hint"])
        u = self.universe
        if name == "order":
            c = u.by_token.get(int(m["token"]))
            if c is None:
                return {"ok": False, "msg": "unknown contract"}
            lots = int(m.get("lots", 0))
            lim = m.get("limit")
            kind = "LMT" if lim not in (None, "", 0) else "MKT"
            return self.live.place(c.token, m["side"], lots * c.lot, kind, float(lim) if kind == "LMT" else None,
                                   source=m.get("source", ""))
        if name == "basket":
            legs = []
            for leg in m.get("legs", []):
                c = u.by_token.get(int(leg["token"]))
                if c is None:
                    return {"ok": False, "msg": "unknown contract in basket"}
                if leg.get("hint"):
                    self.hint(c.token, leg["hint"])
                legs.append({"token": c.token, "side": leg["side"], "qty": int(leg["lots"]) * c.lot})
            if not legs:
                return {"ok": False, "msg": "empty basket"}
            return self.live.basket(legs, source=m.get("source", "basket"))
        if name == "move":
            return self.live.move(int(m["token"]), int(m.get("steps", 1)), bool(m.get("away", True)),
                                  int(m["lots"]) if m.get("lots") else None)
        if name == "cancel":
            return self.live.cancel(int(m["id"]))
        if name == "cancel_all":
            return {"ok": True, "msg": f"cancelled {self.live.cancel_all()} order(s)"}
        if name == "exit":
            return self.live.exit_position(int(m["token"]), float(m.get("frac", 1.0)))
        if name == "exit_all":
            return self.live.exit_all(float(m.get("frac", 1.0)))
        if name == "exit_inst":
            return self.live.exit_instrument(str(m.get("name", "")), float(m.get("frac", 1.0)))
        if name == "sl_set":
            return self.sl.set_one(int(m["token"]), bool(m.get("on", True)), m.get("trigger"), m.get("qty"))
        if name == "sl_drop":
            return self.sl.drop(int(m["token"]))
        if name == "sl_apply":
            return self.sl.apply()
        if name == "risk_set":
            return self.risk.set(m.get("stop"), m.get("target"), m.get("trail"))
        if name == "risk_clear":
            return self.risk.clear()
        if name == "timer_set":
            return self.timers.set(str(m.get("name", "")), m.get("frac", 1.0), m.get("at", ""), "LIVE",
                                   m.get("_user", ""))
        if name == "timer_clear":
            return self.timers.clear(str(m.get("name", "")), m.get("frac", 1.0), m.get("_user", ""))
        if name == "mode_set":
            return self.set_mode(m.get("mode", ""), m.get("_user", ""))
        if name == "watch":
            self.set_watch(m.get("tokens"))
            return {"ok": True, "quiet": True}
        if name == "refresh":
            ok = self.live.poll()
            self.live.sync_positions()
            return {"ok": True, "msg": "Kotak orders and positions re-read" if ok is not False else "re-read sent"}
        return {"ok": False, "msg": f"unknown op {name}"}

    # ── state for the screen ────────────────────────────────────────────────
    def status(self) -> dict:
        return {"armed": bool(self.arm.get("armed")), "arm": self.arm, "broker": self.live.broker,
                "live_open": self.live.open_positions(), "live_working": self.live.working(),
                "pos_sync": getattr(self.live, "pos_sync", {}), "uptime": round(time.time() - self.started_at),
                "feed": self.feed.status() if self.feed else None,
                "order_push": dict(self.orderfeed.status) if self.orderfeed else {},
                "pos_sync_view": {"ok": self.live.positions_fresh(), "msg": self.live.pos_sync.get("msg", ""),
                                  "age": round(time.time() - self.live.pos_sync["at"], 1)
                                  if self.live.pos_sync.get("at") else None}}

    def snapshot(self, kind: str | None = None) -> dict:
        out = {}
        if kind in (None, "book"):
            out["book"] = {**self.live.snapshot(), "sl": self.sl.view()}
        if kind in (None, "risk"):
            out["risk"] = self.risk.state()
        if kind in (None, "timers"):
            out["timers"] = self.timers.view()
        if kind in (None, "status"):
            out["status"] = self.status()
        return out

    # ── push to the screen ──────────────────────────────────────────────────
    def on_change(self, kind: str):
        if kind == "book":
            self._resubscribe_soon()
        if self.loop is None or not self.listeners:
            return
        self.loop.call_soon_threadsafe(self._schedule_push, kind)

    def _resubscribe_soon(self):
        if self.feed and not getattr(self, "_resub_pending", False):
            self._resub_pending = True

            def go():
                self._resub_pending = False
                self._resubscribe()
            threading.Timer(0.2, go).start()

    def _schedule_push(self, kind):
        if kind in self._push_pending:
            return
        wait = max(0.0, PUSH_MIN_S - (time.time() - self._push_at.get(kind, 0)))
        self._push_pending.add(kind)
        self.loop.call_later(wait, self._push, kind)

    def _push(self, kind):
        self._push_pending.discard(kind)
        self._push_at[kind] = time.time()
        try:
            msg = {"t": kind, **self.snapshot(kind)}
            if kind == "status":
                msg["status"] = msg.get("status")
        except Exception:
            log.exception("gateway snapshot %s", kind)
            return
        for q in list(self.listeners):
            if q.qsize() < 200:
                q.put_nowait(msg)

    def on_note(self, ev: dict):
        if self.loop is None:
            return
        msg = {"t": "note", **ev}
        self.loop.call_soon_threadsafe(lambda: [q.put_nowait(msg) for q in list(self.listeners) if q.qsize() < 200])


# ── HTTP / WS on 127.0.0.1 ──────────────────────────────────────────────────
def build_app(gw: Gateway, secret: str):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import JSONResponse
    app = FastAPI(title=f"{C.DESK} order gateway")
    pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="GwOp")

    def ok_secret(given) -> bool:
        return bool(secret) and secrets.compare_digest(str(given or ""), secret)

    @app.on_event("startup")
    async def _startup():
        gw.loop = asyncio.get_running_loop()

    @app.get("/health")
    def health():
        return {"ok": True, "desk": C.DESK, **gw.status()}

    @app.get("/snapshot")
    def snapshot(request: Request):
        if not ok_secret(request.headers.get(SECRET_HEADER)):
            return JSONResponse({"ok": False, "msg": "forbidden"}, status_code=403)
        return gw.snapshot()

    @app.post("/op/{name}")
    async def op(name: str, request: Request):
        if not ok_secret(request.headers.get(SECRET_HEADER)):
            return JSONResponse({"ok": False, "msg": "forbidden"}, status_code=403)
        m = await request.json()
        t0 = time.time()
        try:
            res = await asyncio.get_running_loop().run_in_executor(pool, gw.op, name, m)
        except Exception as e:
            log.exception("gateway op %s", name)
            res = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        if time.time() - t0 > 3:
            log.warning("slow gateway op %s: %.2fs", name, time.time() - t0)
        return res

    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        if not ok_secret(ws.headers.get(SECRET_HEADER)):
            await ws.close(code=4403)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        gw.listeners.add(q)
        try:
            await ws.send_text(json.dumps({"t": "hello", **gw.snapshot()}, default=str))
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            gw.listeners.discard(q)

    return app


def main():
    import uvicorn
    from live_trading_options.mcx_terminal import fastrest
    from live_trading_options.mcx_terminal.kotak_session import SessionHolder
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if C.BROKER != "KOTAK" or not C.LIVE_ORDERS:
        raise SystemExit(f"{C.DESK} desk has no live orders - no gateway")
    fastrest.install()
    secret = load_secret(create=True)
    gw = Gateway(SessionHolder())
    log.info("%s order gateway on 127.0.0.1:%d (armed=%s)", C.DESK, PORT, bool(gw.arm.get("armed")))
    uvicorn.run(build_app(gw, secret), host="127.0.0.1", port=PORT, log_level="warning",
                ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
