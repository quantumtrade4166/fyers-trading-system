"""
platform/tickbus/bus_server.py — single-owner Kite feed + local fan-out.
========================================================================

ONE process owns the single real KiteTicker (the scarce, capped resource) and
re-broadcasts its ticks to any number of local strategy processes. This is what
lets many strategies share one Zerodha websocket connection instead of each
burning one of the 3 allowed per API key.

RELIABILITY (this feeds live-money engines, so correctness beats cleverness):
  * The Kite read loop (`_on_ticks`) NEVER does socket I/O and NEVER raises — it only
    drops each tick onto per-consumer in-memory queues. A slow, stuck or dead consumer
    can therefore never back-pressure the feed or starve the other consumers.
  * Each consumer has its OWN bounded queue + sender thread. Queue full -> drop OLDEST
    (keep the feed live; a laggy consumer loses old ticks, never the whole bus).
  * A consumer socket error drops only that consumer and re-syncs the union subscription.
  * On every Kite (re)connect the FULL union is re-subscribed (Kite forgets subs on
    reconnect) and consumers are told the feed is back.
The server is deliberately dependency-free (stdlib socket/threading) and binds to
127.0.0.1 only.
"""

import socket
import threading
import time
import queue
from pathlib import Path

from .protocol import (DEFAULT_HOST, DEFAULT_PORT, FrameReader, pack,
                       T_HELLO, T_SUBSCRIBE, T_UNSUBSCRIBE, T_TICKS, T_FEED, T_PING, T_PONG,
                       MODE_LTP, MODE_QUOTE, MODE_FULL)

_MODE_RANK = {MODE_LTP: 1, MODE_QUOTE: 2, MODE_FULL: 3}
_RANK_MODE = {v: k for k, v in _MODE_RANK.items()}
_QMAX = 5000                              # per-consumer backlog before drop-oldest


class _Consumer:
    __slots__ = ("cid", "sock", "tokens", "q", "alive")

    def __init__(self, cid, sock):
        self.cid = cid
        self.sock = sock
        self.tokens = {}                  # token(int) -> mode(str)
        self.q = queue.Queue(maxsize=_QMAX)
        self.alive = True


class TickBus:
    def __init__(self, ticker, *, host=DEFAULT_HOST, port=DEFAULT_PORT,
                 heartbeat_path=None, log=print):
        self.ticker = ticker
        self.host, self.port = host, port
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else None
        self.log = log
        self._consumers = {}
        self._next_cid = 1
        self._lock = threading.RLock()
        self._srv = None
        self._stop = threading.Event()
        # own the ticker's callbacks
        self.ticker.on_ticks = self._on_ticks
        self.ticker.on_connect = self._on_connect
        self.ticker.on_close = self._on_close
        self.ticker.on_error = self._on_error

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self, threaded=True):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(32)
        threading.Thread(target=self._accept_loop, name="bus-accept", daemon=True).start()
        if self.heartbeat_path:
            threading.Thread(target=self._heartbeat_loop, name="bus-hb", daemon=True).start()
        self.ticker.connect(threaded=True)     # KiteTicker.connect blocks; run it in its thread
        self.log(f"[tickbus] listening on {self.host}:{self.port}")
        if not threaded:
            try:
                while not self._stop.is_set():
                    time.sleep(0.5)
            except KeyboardInterrupt:
                self.stop()

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except Exception:
            pass

    # ── consumer accept / IO ──────────────────────────────────────────────
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, _ = self._srv.accept()
            except OSError:
                break
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                cid = self._next_cid
                self._next_cid += 1
                c = _Consumer(cid, sock)
                self._consumers[cid] = c
            try:
                sock.sendall(pack({"type": T_HELLO}))
            except Exception:
                self._drop(c)
                continue
            threading.Thread(target=self._reader, args=(c,), name=f"bus-rd-{cid}", daemon=True).start()
            threading.Thread(target=self._sender, args=(c,), name=f"bus-sn-{cid}", daemon=True).start()
            self.log(f"[tickbus] consumer {cid} connected ({len(self._consumers)} total)")

    def _reader(self, c):
        fr = FrameReader()
        try:
            while not self._stop.is_set() and c.alive:
                data = c.sock.recv(65536)
                if not data:
                    break
                for msg in fr.feed(data):
                    self._handle_msg(c, msg)
        except Exception:
            pass
        finally:
            self._drop(c)

    def _sender(self, c):
        try:
            while c.alive and not self._stop.is_set():
                try:
                    payload = c.q.get(timeout=1.0)
                except queue.Empty:
                    continue
                c.sock.sendall(payload)
        except Exception:
            pass
        finally:
            self._drop(c)

    def _drop(self, c):
        with self._lock:
            if c.cid in self._consumers:
                del self._consumers[c.cid]
                c.alive = False
                try:
                    c.sock.close()
                except Exception:
                    pass
                self.log(f"[tickbus] consumer {c.cid} dropped ({len(self._consumers)} left)")
                self._resync_subscription()

    def _handle_msg(self, c, msg):
        t = msg.get("type")
        if t == T_SUBSCRIBE:
            mode = msg.get("mode", MODE_QUOTE)
            if mode not in _MODE_RANK:
                mode = MODE_QUOTE
            with self._lock:
                for tok in msg.get("tokens", []):
                    c.tokens[int(tok)] = mode
                self._resync_subscription()
        elif t == T_UNSUBSCRIBE:
            with self._lock:
                for tok in msg.get("tokens", []):
                    c.tokens.pop(int(tok), None)
                self._resync_subscription()
        elif t == T_PING:
            try:
                c.sock.sendall(pack({"type": T_PONG}))
            except Exception:
                pass

    # ── union subscription on the real ticker ──────────────────────────────
    def _resync_subscription(self):
        """Recompute the token->highest-mode union over all consumers and push it to the
        real ticker. Must be called under _lock."""
        want = {}
        for c in self._consumers.values():
            for tok, mode in c.tokens.items():
                r = _MODE_RANK[mode]
                if r > want.get(tok, 0):
                    want[tok] = r
        tokens = list(want.keys())
        if not tokens:
            return
        try:
            self.ticker.subscribe(tokens)
            by_mode = {}
            for tok, r in want.items():
                by_mode.setdefault(_RANK_MODE[r], []).append(tok)
            for mode, toks in by_mode.items():
                self.ticker.set_mode(self._kite_mode(mode), toks)
        except Exception as e:
            self.log(f"[tickbus] subscribe resync error (will retry on next change/connect): {e}")

    def _kite_mode(self, mode):
        return {MODE_LTP: getattr(self.ticker, "MODE_LTP", "ltp"),
                MODE_QUOTE: getattr(self.ticker, "MODE_QUOTE", "quote"),
                MODE_FULL: getattr(self.ticker, "MODE_FULL", "full")}[mode]

    # ── real ticker callbacks ──────────────────────────────────────────────
    def _on_ticks(self, ws, ticks):
        # HOT PATH. Never raise, never do socket IO here — only enqueue.
        try:
            with self._lock:
                consumers = list(self._consumers.values())
            for c in consumers:
                sub = c.tokens
                if not sub:
                    continue
                mine = [t for t in ticks if t.get("instrument_token") in sub]
                if not mine:
                    continue
                payload = pack({"type": T_TICKS, "ticks": mine})
                try:
                    c.q.put_nowait(payload)
                except queue.Full:
                    try:                               # drop oldest, keep the feed live
                        c.q.get_nowait()
                        c.q.put_nowait(payload)
                    except Exception:
                        pass
        except Exception as e:
            self.log(f"[tickbus] fanout error (ignored): {e}")

    def _on_connect(self, ws, response):
        self.log("[tickbus] Kite feed connected -> (re)subscribing union")
        with self._lock:
            self._resync_subscription()
            consumers = list(self._consumers.values())
        for c in consumers:
            try:
                c.q.put_nowait(pack({"type": T_FEED, "connected": True}))
            except Exception:
                pass

    def _on_close(self, ws, code, reason):
        self.log(f"[tickbus] Kite feed closed: {code} {reason}")

    def _on_error(self, ws, code, reason):
        self.log(f"[tickbus] Kite feed error: {code} {reason}")

    def _heartbeat_loop(self):
        while not self._stop.is_set():
            try:
                self.heartbeat_path.write_text(str(time.time()))
            except Exception:
                pass
            time.sleep(2)
