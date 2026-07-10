"""
core/singleton.py
=================

Cross-process single-instance guard.

The VPS `.venv` python double-launches every script under the system python
(confirmed: identical creation timestamps, the system process is a direct child
of the venv one). That produced TWO dashboards -> two schedulers -> two order
routers / two V2 engines racing on the same token + files. For LIVE trading that
means duplicate orders — unacceptable.

This guard neutralises it in code: whoever acquires the lock first is the ONLY
instance that runs the scheduler / order router / tick engine; the duplicate
detects the held lock and stands down. It works even while the broken venv is
still in place, so it's the safe belt-and-suspenders around the real venv fix.

Implementation: bind a fixed loopback TCP port. The OS guarantees exactly one
holder; if the process dies the port frees automatically (no stale lock files).
"""

import socket

# distinct ports per singleton role (arbitrary, unused high ports)
PORT_SCHEDULER = 47651     # the dashboard instance that runs jobs / orders
PORT_V2_ENGINE = 47652     # the Vwap-Strangle V2 tick engine

_held: dict[int, socket.socket] = {}     # keep sockets alive for process lifetime


def acquire(port: int) -> bool:
    """Try to become the single instance for `port`. Returns True if acquired
    (this process is now THE instance), False if another instance holds it.
    The socket is kept open for the life of the process."""
    if port in _held:
        return True
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Windows: SO_REUSEADDR=0 is NOT enough to guarantee an exclusive bind (two
    # processes can still both bind). SO_EXCLUSIVEADDRUSE enforces true exclusivity,
    # so the second (duplicate) process's bind fails and it stands down.
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        except OSError:
            pass
    else:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
        _held[port] = s
        return True
    except OSError:
        s.close()
        return False
