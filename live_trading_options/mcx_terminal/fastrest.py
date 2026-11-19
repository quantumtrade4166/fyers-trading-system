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
import ssl
import threading
import time
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("mcx.fastrest")

CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 8.0


_SSL_CTX = None
_SSL_LOCK = threading.Lock()


def ssl_ctx() -> ssl.SSLContext:
    """ONE TLS context for every Kotak connection (REST and both websockets), built once from certifi's CA file.
    18-Nov-2026 21:12-21:18: every NEW connection made Python read the Windows certificate store
    (load_default_certs / cert_verify) while holding the GIL; with a flaky network that hung and froze the whole
    engine for 7-35 s (the engine itself at 2 % CPU). Verification is unchanged: CERT_REQUIRED + hostname check."""
    global _SSL_CTX
    with _SSL_LOCK:
        if _SSL_CTX is None:
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        return _SSL_CTX


class _CtxAdapter(HTTPAdapter):
    """requests adapter that hands urllib3 the shared context and does not make it re-read a CA bundle per
    connection (requests' cert_verify would set ca_certs, and urllib3 would load it into every new socket)."""

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = ssl_ctx()
        return super().init_poolmanager(*a, **kw)

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, verify, cert)
        if verify is not False and url.lower().startswith("https"):
            conn.ca_certs = None                  # the CAs are already in the shared context
            conn.ca_cert_dir = None


class _Shim:
    """Looks like the `requests` module to the SDK: post / get / exceptions / everything else.

    ONE Session for the whole process (18-Nov-2026). It used to be one per thread, but orders run on the web
    server's worker threads, so most orders landed on a thread whose connection was cold or long idle and paid
    a fresh TCP + TLS handshake: MCX place_order took a median 357 ms on 17-Nov (45-65 ms when warm). Now the
    positions poll (every 2 s, same Kotak host) keeps the pool's connections warm and every order reuses one.
    urllib3's connection pool is thread-safe; Kotak auth travels in headers, not cookies."""

    def __init__(self):
        s = requests.Session()
        ad = _CtxAdapter(pool_connections=4, pool_maxsize=16, max_retries=0)
        s.mount("https://", ad)
        self._session = s

    def _s(self) -> requests.Session:
        return self._session

    def post(self, url, **kw):
        kw.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
        _note_host(url)                          # orders / cancels / modifies are POSTs: only their host is warmed
        return self._s().post(url, **kw)

    def get(self, url, **kw):
        kw.setdefault("timeout", (CONNECT_TIMEOUT, READ_TIMEOUT))
        return self._s().get(url, **kw)

    def __getattr__(self, name):                 # requests.exceptions etc.
        return getattr(requests, name)


_done = False
_shim = None
_hosts: set = set()


def _note_host(url):
    try:
        h = urllib.parse.urlsplit(url).netloc
        if h:
            _hosts.add(h)
    except Exception:
        pass


def warm(n: int = 3, timeout: float = 3.0) -> int:
    """Keep `n` connections to every Kotak host open and ready (18-Nov-2026: EXIT ALL's 4 parallel exits took
    0.5-0.6 s each to be accepted - each opened a NEW TLS connection - against ~50 ms on a warm one). n tiny
    requests at the same time make the pool hold n live connections. -> how many answered."""
    if _shim is None or not _hosts:
        return 0
    ok = [0]

    def one(h):
        try:
            _shim._s().head(f"https://{h}/", timeout=(timeout, timeout), allow_redirects=False)
            ok[0] += 1
        except Exception:
            pass
    # 18-Nov-2026 21:09: 6 connections x EVERY Kotak host every 20 s = a dozen threads parsing HTTP at once,
    # which froze the engine 0.5-1.9 s each time. Now: only the order host(s), n connections, started 0.2 s apart.
    ths = []
    for h in list(_hosts)[:2]:
        for _ in range(n):
            t = threading.Thread(target=one, args=(h,), daemon=True, name="KotakWarmOne")
            t.start()
            ths.append(t)
            time.sleep(0.2)
    for t in ths:
        t.join(timeout + 1)
    return ok[0]


def install():
    global _done
    if _done:
        return
    try:
        from neo_api_client import rest
        global _shim
        _shim = _Shim()
        rest.requests = _shim
        _done = True
        log.info("Kotak REST: keep-alive session + %.0fs/%.0fs timeouts installed", CONNECT_TIMEOUT, READ_TIMEOUT)
    except Exception:
        log.exception("could not install the fast Kotak REST shim (SDK calls stay as they were)")


def is_timeout(text: str) -> bool:
    t = str(text).lower()
    return "timeout" in t or "timed out" in t
