"""
exchanges/base.py — the reconnect loop both exchange clients share.
==================================================================

Exchange-specific things (URLs, subscribe messages, message formats) live in
delta.py / binance.py. This file only knows how to keep ONE socket alive:

  * connect, run the client's on_open (subscribe), then read forever
  * a recv that waits longer than STALE_SOCKET_S is a dead socket -> rebuild
    (catches the silent half-open TCP that never raises on its own)
  * every failure -> mark DISCONNECTED, back off 1, 2, 4 … 60 s, reconnect
  * a malformed message is counted and skipped, never fatal
  * protocol pings/pongs are answered by the websockets library itself

Each client runs as its own asyncio task, so one exchange being down (or in
maintenance) never stops the other from updating.
"""

import asyncio
import json
import time

import websockets

from core.orderbook import ResyncRequired

BACKOFF_MIN, BACKOFF_MAX = 1, 60


async def run_socket(name: str, url: str, md, log, on_open, on_message,
                     stale_s: float, stop: asyncio.Event, on_close=None):
    delay = BACKOFF_MIN
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                          close_timeout=5, open_timeout=15,
                                          max_size=2 ** 22) as ws:
                await on_open(ws)
                md.set_connected(name, True)
                log.info(f"{name}: CONNECTED {url}")
                connected_at = time.monotonic()
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=stale_s)
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        md.feed(name).rejected += 1
                        continue
                    try:
                        on_message(msg)
                    except ResyncRequired:
                        raise                       # local book untrustworthy: rebuild socket
                    except Exception as e:          # a bad message must not kill the feed
                        f = md.feed(name)
                        f.rejected += 1
                        f.last_error = f"parse: {type(e).__name__}: {e}"[:300]
                    # only reset the back-off once the link has proved itself
                    if delay != BACKOFF_MIN and time.monotonic() - connected_at > 30:
                        delay = BACKOFF_MIN
        except asyncio.CancelledError:
            raise
        except ResyncRequired as e:
            err = f"order book resync: {e}"
            md.set_connected(name, False, err)
            log.warning(f"{name}: {err}")
        except asyncio.TimeoutError:
            err = f"no message for {stale_s:.0f}s — rebuilding socket"
            md.set_connected(name, False, err)
            log.warning(f"{name}: {err}")
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            md.set_connected(name, False, err)
            log.warning(f"{name}: DISCONNECTED ({err}) — reconnecting in {delay}s")
        else:
            md.set_connected(name, False, "stopped")
        md.clear_depth(name)            # a book from a dead socket is not a book
        if on_close:
            on_close()
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        delay = min(delay * 2, BACKOFF_MAX)
