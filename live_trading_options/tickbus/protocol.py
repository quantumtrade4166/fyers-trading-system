"""
platform/tickbus/protocol.py — wire format for the local tick bus.
==================================================================

A length-prefixed PICKLE frame over a localhost-only TCP socket. Pickle is chosen
deliberately: consumers must receive the EXACT tick dicts KiteTicker delivers
(including datetime fields like `exchange_timestamp`), so there is zero behavioural
drift between an engine on a real KiteTicker and the same engine on a BusTicker.

Pickle is only safe between trusted peers; the bus binds to 127.0.0.1 ONLY, so the
only writers are this machine's own strategy processes. Nothing external can connect.

Frame = 4-byte big-endian unsigned length  +  pickle(payload dict).
"""

import pickle
import struct

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 47670                      # free on the VPS (47651/52 venv, 47656/61/62/64/68 taken)

# message types
T_HELLO = "hello"                         # bus -> client, on accept
T_SUBSCRIBE = "subscribe"                 # client -> bus {tokens:[int], mode:str}
T_UNSUBSCRIBE = "unsubscribe"             # client -> bus {tokens:[int]}
T_TICKS = "ticks"                         # bus -> client {ticks:[dict]}
T_FEED = "feed"                           # bus -> client {connected:bool}
T_PING = "ping"                           # client -> bus
T_PONG = "pong"                           # bus -> client

# subscription modes (mirror KiteTicker's three modes, ranked LTP < QUOTE < FULL)
MODE_LTP = "ltp"
MODE_QUOTE = "quote"
MODE_FULL = "full"

_MAX_FRAME = 64 * 1024 * 1024             # 64 MB guard against a corrupt length header


def pack(msg: dict) -> bytes:
    body = pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)
    return struct.pack("!I", len(body)) + body


class FrameReader:
    """Accumulates socket bytes and yields complete decoded frames. Never raises on a
    bad frame — it skips it, so one corrupt message can never kill a read loop."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes):
        self._buf.extend(data)
        out = []
        while len(self._buf) >= 4:
            (n,) = struct.unpack("!I", self._buf[:4])
            if n > _MAX_FRAME:
                # corrupt/desynced stream — drop one byte and resync rather than hang
                del self._buf[0]
                continue
            if len(self._buf) < 4 + n:
                break
            body = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            try:
                out.append(pickle.loads(body))
            except Exception:
                continue
        return out
