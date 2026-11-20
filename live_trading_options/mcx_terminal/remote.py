"""
mcx_terminal/remote.py - the SCREEN process's side of the order gateway (see GATEWAY_DESIGN.md).

GatewayLink  - HTTP calls to the gateway on 127.0.0.1 (keep-alive) + a background websocket that receives the
               gateway's live book / stop-losses / MTM guard / timers / status as they change.
RemoteLive   - stands in for the LIVE book inside the screen: everything the screen READS (snapshot,
               positions, tokens, counts, broker / position-sync status) comes from the latest push; exits
               called by the screen itself (paper timers never; old timer items) are forwarded.
RemoteSL / RemoteRisk - the same for the LIVE stop-loss column and the LIVE MTM guard.

If the gateway does not answer, `up` is False: LIVE actions return a clear "ORDERS OFFLINE" refusal at once
(never a hang) and the screen shows the red banner. PAPER is never affected.
"""
import json
import logging
import threading
import time

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.remote")
OP_TIMEOUT_S = 30.0           # an EXIT ALL can take ~10 s while it verifies flat at Kotak
HEALTH_TIMEOUT_S = 2.0
STALE_S = 6.0                 # no message from the gateway stream this long (it pings) -> treated as down
OFFLINE = ("ORDERS OFFLINE - the order gateway is not answering. LIVE orders are NOT sent. "
           "Square off in the Kotak app if needed.")


class GatewayLink:
    def __init__(self, port=None, secret=None, on_update=None, start=True):
        from live_trading_options.mcx_terminal import gateway as G
        self.port = port or G.PORT
        self.secret = secret if secret is not None else G.load_secret()
        self.header = G.SECRET_HEADER
        self.base = f"http://127.0.0.1:{self.port}"
        self.on_update = on_update or (lambda kind, msg: None)
        self.state = {"book": {"positions": [], "orders": [], "sl": {}}, "risk": {"armed": False},
                      "timers": [], "status": {}}
        self.last_msg_at = 0.0
        self.connected = False
        self._local = threading.local()
        self._stop = False
        if start:
            threading.Thread(target=self._stream_loop, daemon=True, name="GatewayStream").start()

    # ── calls ───────────────────────────────────────────────────────────────
    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            import requests
            s = self._local.s = requests.Session()
        return s

    @property
    def up(self) -> bool:
        return self.connected and time.time() - self.last_msg_at < STALE_S

    def op(self, name: str, m: dict, timeout: float = OP_TIMEOUT_S) -> dict:
        if not self.secret:
            return {"ok": False, "msg": OFFLINE + " (no gateway secret on this machine)"}
        if not self.up and name not in ("refresh",):
            return {"ok": False, "msg": OFFLINE}
        try:
            r = self._session().post(f"{self.base}/op/{name}", data=json.dumps(m, default=str),
                                     headers={self.header: self.secret, "content-type": "application/json"},
                                     timeout=(1.0, timeout))
            if r.status_code != 200:
                return {"ok": False, "msg": f"order gateway answered HTTP {r.status_code}"}
            return r.json()
        except Exception as e:
            log.warning("gateway op %s failed: %s", name, e)
            return {"ok": False, "msg": f"{OFFLINE} ({type(e).__name__})"}

    # ── the live state stream ───────────────────────────────────────────────
    def _apply(self, msg: dict):
        t = msg.get("t")
        self.last_msg_at = time.time()
        if t == "hello":
            for k in ("book", "risk", "timers", "status"):
                if k in msg:
                    self.state[k] = msg[k]
                    self.on_update(k, msg[k])
        elif t in ("book", "risk", "timers", "status"):
            if t in msg:
                self.state[t] = msg[t]
                self.on_update(t, msg[t])
        elif t == "note":
            self.on_update("note", msg)

    def _stream_loop(self):
        import websocket
        while not self._stop:
            try:
                if not self.secret:
                    from live_trading_options.mcx_terminal import gateway as G
                    self.secret = G.load_secret()
                    if not self.secret:
                        time.sleep(2)
                        continue
                ws = websocket.create_connection(f"ws://127.0.0.1:{self.port}/stream",
                                                 header=[f"{self.header}: {self.secret}"], timeout=10)
                self.connected = True
                self.on_update("status", self.state.get("status") or {})
                log.info("order gateway stream connected (127.0.0.1:%d)", self.port)
                ws.settimeout(2.0)
                while not self._stop:
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        if time.time() - self.last_msg_at > 2.0:
                            self._health_ping()
                        continue
                    if not raw:
                        break
                    self._apply(json.loads(raw))
            except Exception as e:
                if self.connected:
                    log.warning("order gateway stream lost: %s", e)
            was = self.connected
            self.connected = False
            if was:
                self.on_update("status", self.state.get("status") or {})
            time.sleep(1.0)

    def _health_ping(self):
        """Quiet stream: ask /health so `up` means 'answering now', not 'answered once'."""
        try:
            r = self._session().get(f"{self.base}/health", timeout=HEALTH_TIMEOUT_S)
            if r.status_code == 200:
                self.last_msg_at = time.time()
                st = r.json()
                prev = self.state.get("status") or {}
                st = {k: v for k, v in st.items() if k not in ("ok", "desk")}
                if st.get("armed") != prev.get("armed"):
                    self.state["status"] = {**prev, **st}
                    self.on_update("status", self.state["status"])
        except Exception:
            pass


class RemoteLive:
    """The LIVE book as the screen sees it (gateway owns the real one)."""

    def __init__(self, link: GatewayLink):
        self.link = link
        self.lock = threading.RLock()
        self.notify = None
        self.on_push_other = None
        self.sl_ids = None
        self.before_reduce = None
        self.before_flatten = None

    # what the screen reads
    @property
    def _book(self) -> dict:
        return self.link.state.get("book") or {}

    @property
    def pos(self) -> dict:
        return {int(p["token"]): p for p in self._book.get("positions", []) if p.get("token") is not None}

    def snapshot(self) -> dict:
        b = self._book
        return {"positions": [dict(p) for p in b.get("positions", [])], "orders": [dict(o) for o in b.get("orders", [])]}

    def positions(self) -> list:
        return [dict(p) for p in self._book.get("positions", [])]

    def tokens(self) -> set:
        b = self._book
        return ({int(p["token"]) for p in b.get("positions", []) if p.get("net")} |
                {int(o["token"]) for o in b.get("orders", []) if o.get("status") in ("OPEN", "SENDING")})

    def _status(self) -> dict:
        return self.link.state.get("status") or {}

    def open_positions(self) -> int:
        return int(self._status().get("live_open") or 0)

    def working(self) -> int:
        return int(self._status().get("live_working") or 0)

    @property
    def broker(self) -> dict:
        if not self.link.up:
            return {"ok": False, "msg": "order gateway not answering", "at": time.time()}
        return self._status().get("broker") or {"ok": True}

    @property
    def pos_sync(self) -> dict:
        v = self._status().get("pos_sync") or {}
        return v if isinstance(v, dict) else {}

    def positions_fresh(self) -> bool:
        v = self._status().get("pos_sync_view") or {}
        return bool(v.get("ok")) and self.link.up

    def pos_sync_view(self) -> dict:
        return self._status().get("pos_sync_view") or {}

    # no-ops: the gateway runs these
    def housekeep(self):
        pass

    def on_tick(self, token):
        pass

    # forwarded (the screen itself never trades LIVE except through run_op; these cover old timer items)
    def poll(self):
        return self.link.op("refresh", {}).get("ok", False)

    def sync_positions(self, raise_errors=False):
        return True

    def exit_instrument(self, name, frac=1.0):
        return self.link.op("exit_inst", {"name": name, "frac": frac})

    def exit_all(self, frac=1.0):
        return self.link.op("exit_all", {"frac": frac})

    def exit_position(self, token, frac=1.0):
        return self.link.op("exit", {"token": token, "frac": frac})

    def cancel_all(self):
        r = self.link.op("cancel_all", {})
        return r.get("msg", "")


class RemoteSL:
    def __init__(self, link: GatewayLink):
        self.link = link

    def view(self) -> dict:
        return (self.link.state.get("book") or {}).get("sl") or {}

    def working_ids(self, name=None):
        return []

    def before_reduce(self, token):
        return None

    def before_flatten(self):
        pass

    def on_push(self, row):
        pass

    def start(self):
        pass

    def reset(self):
        return {"ok": True}


class RemoteRisk:
    def __init__(self, link: GatewayLink):
        self.link = link

    def state(self) -> dict:
        return self.link.state.get("risk") or {"armed": False}

    def check(self):
        return None

    def reset(self):
        return {"ok": True}
