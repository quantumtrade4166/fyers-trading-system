"""
mcx_terminal/fastrest.py — make the Kotak SDK's REST calls fast and bounded (this process only).
===============================================================================================

The Neo SDK sends every request with a bare `requests.post/get` -> a NEW TCP + TLS connection per
order / poll (adds ~100-250 ms each) and NO timeout (27-Oct: one place_order hung 60 s and pushed the
rest of EXIT ALL past the close). install() swaps the module the SDK's rest.py uses for a shim that
sends through ONE keep-alive requests.Session with a timeout. Only this process is affected; the
shared venv and every other engine stay as they are.
"""
import logging

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("mcx.fastrest")

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 8.0


class _Shim:
    """Looks like the `requests` module to the SDK: post / get / exceptions / everything else.

    ONE Session for the whole process (18-Nov-2026). It used to be one per thread, but orders run on the web
    server's worker threads, so most orders landed on a thread whose connection was cold or long idle and paid
    a fresh TCP + TLS handshake: MCX place_order took a median 357 ms on 17-Nov (45-65 ms when warm). Now the
    positions poll (every 2 s, same Kotak host) keeps the pool's connections warm and every order reuses one.
    urllib3's connection pool is thread-safe; Kotak auth travels in headers, not cookies."""

    def __init__(self):
        s = requests.Session()
        ad = HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
        s.mount("https://", ad)
        self._session = s

    def _s(self) -> requests.Session:
        return self._session

    def post(self, url, **kw):
        kw.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
        return self._s().post(url, **kw)

    def get(self, url, **kw):
        kw.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
        return self._s().get(url, **kw)

    def __getattr__(self, name):                 # requests.exceptions etc.
        return getattr(requests, name)


_done = False


def install():
    global _done
    if _done:
        return
    try:
        from neo_api_client import rest
        rest.requests = _Shim()
        _done = True
        log.info("Kotak REST: keep-alive session + %.0fs/%.0fs timeouts installed", CONNECT_TIMEOUT, READ_TIMEOUT)
    except Exception:
        log.exception("could not install the fast Kotak REST shim (SDK calls stay as they were)")


def is_timeout(text: str) -> bool:
    t = str(text).lower()
    return "timeout" in t or "timed out" in t
