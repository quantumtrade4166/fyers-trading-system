"""
mcx_terminal/orderfeed.py — Kotak's ORDER-UPDATE push channel (instant fills).
=============================================================================

Kotak pushes every order status change ("open", "complete", "rejected", ...) and every position change
over wss://<data-center>.kotaksecurities.com/realtime the moment it happens. Measured 28-Oct-2026 on 4
real 1-lot CRUDEOIL orders: the push arrived 0.06-0.13 s before a 0.1 s order-book poll saw the fill.

This only SPEEDS UP the screen. The order-book poll in live.py stays on as the backup and the truth:
if this channel drops, nothing is lost - fills just show at poll speed until it reconnects.
Login tokens are never written to the log.
"""
import json
import logging
import threading
import time

import websocket                                    # websocket-client

log = logging.getLogger("mcx.orderfeed")

URLS = {"": "wss://mis.kotaksecurities.com/realtime", "adc": "wss://cis.kotaksecurities.com/realtime",
        "e21": "wss://e21.kotaksecurities.com/realtime", "e22": "wss://e22.kotaksecurities.com/realtime",
        "e41": "wss://e41.kotaksecurities.com/realtime", "e43": "wss://e43.kotaksecurities.com/realtime"}


def parse(raw):
    """-> ("order" | "position" | "cn" | None, data dict)"""
    try:
        m = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
    except Exception:
        return None, {}
    if not isinstance(m, dict):
        return None, {}
    t = m.get("type")
    if t in ("order", "position") and isinstance(m.get("data"), dict):
        return t, m["data"]
    if m.get("type") == "cn" and m.get("ak") == "ok":
        return "cn", m
    return None, m


class OrderFeed:
    def __init__(self, sessions, on_order, on_position=None, on_reconnect=None):
        self.sessions = sessions
        self.on_order = on_order
        self.on_position = on_position
        self.on_reconnect = on_reconnect            # after a (re)connect: catch up with one order-book read
        self.status = {"ok": False, "msg": "starting", "at": None, "msgs": 0, "reconnects": 0}
        self.ws = None
        self._stop = False

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="KotakOrderFeed").start()
        return self

    def stop(self):
        self._stop = True
        try:
            self.ws and self.ws.close()
        except Exception:
            pass

    def _url_and_auth(self):
        cfg = self.sessions.get().client.configuration
        dc = str(getattr(cfg, "data_center", "") or "").lower()
        return URLS.get(dc, URLS[""]), cfg.edit_token, cfg.edit_sid

    def _run(self):
        wait = 1.0
        while not self._stop:
            t0 = time.time()
            try:
                url, token, sid = self._url_and_auth()
                if not token or not sid:
                    raise RuntimeError("no Kotak session token yet")

                def on_open(ws):
                    # same unquoted format the Kotak SDK sends
                    ws.send("{type:cn,Authorization:%s,Sid:%s,src:WEB}" % (token, sid))

                self.ws = websocket.WebSocketApp(url, on_open=on_open, on_message=self._on_msg,
                                                 on_error=lambda w, e: self._down(f"error: {e}"),
                                                 on_close=lambda w, c, r: self._down("closed"))
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                self._down(f"cannot connect: {e}")
            if self._stop:
                return
            if time.time() - t0 > 60:               # it had been up a while: retry fast
                wait = 1.0
            time.sleep(wait)
            wait = min(wait * 2, 15.0)
            self.status["reconnects"] += 1

    def _down(self, why):
        if self.status["ok"]:
            log.warning("Kotak order push channel down (%s) - fills fall back to order-book polling", why)
        self.status.update(ok=False, msg=str(why)[:160], at=time.time())

    def _on_msg(self, ws, raw):
        kind, d = parse(raw)
        if kind == "cn":
            log.info("Kotak order push channel connected")
            self.status.update(ok=True, msg="", at=time.time())
            if self.on_reconnect:
                try:
                    self.on_reconnect()
                except Exception:
                    log.exception("order feed catch-up")
            return
        if kind is None:
            return                                   # acks / heartbeat replies
        self.status["msgs"] += 1
        self.status["at"] = time.time()
        try:
            if kind == "order":
                self.on_order(d)
            elif self.on_position:
                self.on_position(d)
        except Exception:
            log.exception("order push handler")
