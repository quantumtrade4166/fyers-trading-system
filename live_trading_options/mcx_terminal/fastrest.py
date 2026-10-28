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
import threading

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("mcx.fastrest")

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 8.0


class _Shim:
    """Looks like the `requests` module to the SDK: post / get / exceptions / everything else."""

    def __init__(self):
        self._local = threading.local()          # one Session per thread (Session is not thread-safe to share)

    def _s(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            ad = HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
            s.mount("https://", ad)
            self._local.s = s
        return s

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
