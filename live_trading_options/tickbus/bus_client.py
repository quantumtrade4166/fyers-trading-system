"""
platform/tickbus/bus_client.py — BusTicker: a drop-in replacement for KiteTicker.
=================================================================================

An engine migrates onto the shared feed by changing ONE line:
    from kiteconnect import KiteTicker        ->  from ...tickbus.bus_client import BusTicker
    kws = KiteTicker(api_key, access_token)   ->  kws = BusTicker(api_key, access_token)
and nothing else — the attribute callbacks (on_ticks/on_connect/on_close/on_error),
the methods (subscribe / set_mode / connect) and the MODE_* constants match KiteTicker,
and on_ticks receives the identical list of tick dicts. To revert, change the line back.

It connects to the local bus and AUTO-RECONNECTS with backoff, re-subscribing its tokens
each time — so a bus restart is a brief gap, never a dead engine (same resilience the
real KiteTicker gives against a Zerodha socket drop).
"""

import socket
import threading
import time

from .protocol import (DEFAULT_HOST, DEFAULT_PORT, FrameReader, pack,
                       T_SUBSCRIBE, T_TICKS, T_PING, MODE_LTP, MODE_QUOTE, MODE_FULL)


class BusTicker:
    MODE_LTP = MODE_LTP
    MODE_QUOTE = MODE_QUOTE
    MODE_FULL = MODE_FULL

    def __init__(self, api_key=None, access_token=None, *,
                 host=DEFAULT_HOST, port=DEFAULT_PORT, reconnect=True, log=print):
        # api_key/access_token are accepted for signature-compatibility with KiteTicker;
        # the BUS owns the real Kite session, so the client does not use them.
        self.api_key = api_key
        self.host, self.port = host, port
        self._reconnect = reconnect
        self.log = log
        # KiteTicker-compatible callbacks (assigned by the engine)
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None
        self._desired = {}                 # token(int) -> mode(str)
        self._sock = None
        self._lock = threading.RLock()
        self._stop = threading.Event()

    # ── KiteTicker-compatible API ──────────────────────────────────────────
    def subscribe(self, tokens):
        with self._lock:
            for t in tokens:
                self._desired.setdefault(int(t), MODE_QUOTE)
        self._send_subscribe(tokens, None)

    def set_mode(self, mode, tokens):
        with self._lock:
            for t in tokens:
                self._desired[int(t)] = mode
        self._send_subscribe(tokens, mode)

    def connect(self, threaded=False, **_):
        if threaded:
            threading.Thread(target=self._run, name="busticker", daemon=True).start()
        else:
            self._run()

    def close(self, *_):
        self._stop.set()
        with self._lock:
            if self._sock:
                try:
                    self._sock.close()
                except Exception:
                    pass

    stop = close                           # KiteTicker exposes both in some versions

    # ── internals ──────────────────────────────────────────────────────────
    def _send_subscribe(self, tokens, mode):
        toks = [int(t) for t in tokens]
        if not toks:
            return
        with self._lock:
            if mode is None:
                groups = {}
                for t in toks:
                    groups.setdefault(self._desired.get(t, MODE_QUOTE), []).append(t)
            else:
                groups = {mode: toks}
            have_sock = self._sock is not None
        if not have_sock:
            return                         # queued in _desired; sent on (re)connect
        for m, ts in groups.items():
            self._safe_send(pack({"type": T_SUBSCRIBE, "tokens": ts, "mode": m}))

    def _safe_send(self, payload):
        with self._lock:
            s = self._sock
        if s is None:
            return
        try:
            s.sendall(payload)
        except Exception:
            pass

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            try:
                s = socket.create_connection((self.host, self.port), timeout=5)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with self._lock:
                    self._sock = s
                backoff = 0.5
                # (re)send all desired subscriptions grouped by mode
                with self._lock:
                    desired = dict(self._desired)
                groups = {}
                for t, m in desired.items():
                    groups.setdefault(m, []).append(t)
                for m, ts in groups.items():
                    self._safe_send(pack({"type": T_SUBSCRIBE, "tokens": ts, "mode": m}))
                if self.on_connect:
                    try:
                        self.on_connect(self, {})
                    except Exception as e:
                        self._err(e)
                self._read_loop(s)
            except Exception as e:
                self._err(e)
            with self._lock:
                self._sock = None
            if self.on_close:
                try:
                    self.on_close(self, None, "bus disconnected")
                except Exception:
                    pass
            if not self._reconnect or self._stop.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    def _read_loop(self, s):
        fr = FrameReader()
        while not self._stop.is_set():
            data = s.recv(65536)
            if not data:
                break
            for msg in fr.feed(data):
                if msg.get("type") == T_TICKS and self.on_ticks:
                    try:
                        self.on_ticks(self, msg["ticks"])
                    except Exception as e:
                        self._err(e)       # a consumer bug never kills its own read loop

    def _err(self, e):
        if self.on_error:
            try:
                self.on_error(self, None, str(e))
            except Exception:
                pass
