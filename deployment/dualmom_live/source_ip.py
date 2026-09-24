"""
Bound every Kotak call, and (optionally) pin it to a specific source IP.

TIMEOUTS — always on
    neo_api_client calls requests with no timeout at all. On 2026-09-21 a limits()
    call sat in the TLS handshake indefinitely and every dashboard request queued
    behind the shared-session lock. install() therefore always wraps
    requests.Session.request to put a (connect, read) bound on every
    *.kotaksecurities.com call. This half of the module is not optional.

TWO KOTAK ACCOUNTS SHARE THIS VPS — read this before touching the pin
    Kotak Rohit   UCC 15P5***  KOTAK_DM_*  DualMom, this package, its own service
    "Kotak bhaiya" UCC 15S0***  KOTAK_*     the Vwap Strangle mirror leg

    They are different accounts with different consumer keys, so Kotak's one-session
    limit applies to each separately and neither can end the other's session.

    The strangle's live/kotak_auth.py has never pinned anything — it has always gone
    out of the VPS's own 144.79.166.103. Since 2026-09-24 DualMom does too, so BOTH
    accounts now log in from that one address. That is fine as observed (each API app
    has its own registered IP list), but it is the first thing to check if Kotak ever
    starts refusing one of them: the answer is to register the IP for that account,
    NOT to resurrect a source-IP pin, which cannot work from this VPS (see below).

SOURCE-IP PIN — off by default, and never fatal
    The original design pinned Kotak Rohit to a secondary address, 103.49.131.3, on
    the belief that Kotak binds a whitelisted IP to ONE client and that 144.79.166.103
    was already spoken for by the strangle's account.

    That address is a secondary IP registered WITH KOTAK for the DualMom account —
    103.49.131.0/24 is an Indian "Private Customer" range, not the VPS provider's
    (144.79.166.0/24, NEXTA WEB). Registering an IP with a broker means the broker
    ACCEPTS calls arriving from it; it does not give this machine the right to send
    from it. It worked at go-live on 2026-09-15 (the 1-share IDEA test filled through
    it) and had stopped routing by 2026-09-24.

    On 2026-09-24 the design turned out to be wrong twice over:

      * 103.49.131.3 is a /32 manually added to the NIC but NOT part of the VPS's
        144.79.166.0/24 network. Nothing routes it. Binding to it and connecting to
        Kotak timed out on 5 attempts out of 5 (10s each), while the default IP
        connected in 0.03s. Because bind() still succeeds for an address that is on
        a NIC, the old _ip_is_local() check passed and the pin was installed anyway
        — so every login hung for 10s and the dashboard blanked with
        ApiException: ConnectTimeout.

      * A full totp_login + totp_validate from the DEFAULT IP succeeded in ~1s and
        holdings() returned all 40 live positions. 144.79.166.103 is whitelisted for
        UCC 15P56, so the pin was never needed.

    A pin is now only installed if it DEMONSTRABLY WORKS: the candidate address must
    both be local and complete a TCP connection to Kotak from that address. If it
    cannot, the pin is skipped with a loud warning and traffic leaves from the
    default IP, which is what the account accepts. A dead pin must degrade to a
    working login, never to a total outage.

    Set KOTAK_DM_SOURCE_IP to an address to request a pin; leave it unset (the
    default), or "none"/"auto"/"off", for no pin.

HOW THE PIN WORKS
    neo_api_client makes plain module-level requests.get/post calls (no Session), so
    there is no per-client object to configure. Instead we wrap urllib3's
    create_connection — the single point every requests call goes through — and add
    source_address ONLY when the destination host is *.kotaksecurities.com.

    Everything else in the same process is untouched on purpose:
      * the Fyers data refresh (api-t1.fyers.in) keeps leaving from the default IP
      * localhost calls (the dashboard proxy -> :8010) must never be bound to a
        public address, or they fail outright

SCOPE — the pin is a PROCESS-wide patch
    If this process also ran the strangle's Kotak client, that client talks to the
    same *.kotaksecurities.com hosts and would get pinned too — sending the
    strangle's orders from Kotak Rohit's IP. A pin therefore REFUSES to install in
    any process that has the strangle's Kotak modules loaded. Today only the DualMom
    service (and scripts run by hand) log in as Kotak Rohit, and the dashboard
    reaches DualMom through a proxy, so they never share a process.
"""

import os
import socket
import sys
import threading

# No pin by default: 144.79.166.103 is whitelisted for UCC 15P56 (proven by a full
# login + 40-row holdings() on 2026-09-24). Set KOTAK_DM_SOURCE_IP to request one.
SOURCE_IP = os.getenv("KOTAK_DM_SOURCE_IP", "").strip()
_NO_PIN = {"", "none", "auto", "off", "default", "0"}
PIN_SUFFIX = "kotaksecurities.com"
KOTAK_TIMEOUT = (10, 30)   # (connect, read) seconds for every Kotak HTTP call

# How long to let the pre-flight reachability probe run. It must be shorter than the
# connect timeout above, so a dead pin is detected once at startup instead of costing
# every later call a full 10s hang.
PROBE_TIMEOUT = 4.0
PROBE_HOST = "mis.kotaksecurities.com"
PROBE_PORT = 443

# modules that belong to the STRANGLE's Kotak leg — never share a process with them
_STRANGLE_MARKERS = ("live.kotak_auth", "kotak_executor", "kotak_controller",
                     "live_tick_engine")

_lock = threading.Lock()
_installed_ip = None
_installed = False  # the timeout wrapper is in place (with or without a pin)
_pin_skipped = None  # why a requested pin was not installed, for /status
_seen = []          # (host, local_ip) of recent pinned connections, for verification


def _host_is_kotak(host) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return h == PIN_SUFFIX or h.endswith("." + PIN_SUFFIX)


def _strangle_loaded() -> list:
    return sorted(m for m in list(sys.modules)
                  if any(m.endswith(x) or f".{x}" in m for x in _STRANGLE_MARKERS)
                  and "dualmom" not in m)


def _ip_is_local(ip: str) -> bool:
    """True if `ip` is assigned to a NIC on this machine.

    NOT proof that anything routes FROM it — 103.49.131.3 passed this check for days
    while every connection out of it timed out. Always pair it with _pin_reaches().
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _pin_reaches_kotak(ip: str) -> tuple:
    """Can we actually open a TCP connection to Kotak FROM `ip`? -> (ok, detail).

    This is the check the old fail-closed logic was missing. An address can sit on
    the NIC and still be unroutable: if it is not part of a network this host is
    allowed to source from, the SYN is dropped upstream (or the reply is routed to
    whoever really owns the address) and the connect hangs to its full timeout.
    """
    try:
        infos = socket.getaddrinfo(PROBE_HOST, PROBE_PORT, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as e:
        return False, f"cannot resolve {PROBE_HOST}: {e}"
    if not infos:
        return False, f"no A record for {PROBE_HOST}"

    dest = infos[0][4]
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(PROBE_TIMEOUT)
    try:
        s.bind((ip, 0))
    except OSError as e:
        s.close()
        return False, f"cannot bind to {ip}: {e}"
    try:
        s.connect(dest)
        return True, f"connected to {dest[0]}:{dest[1]} from {ip}"
    except socket.timeout:
        return False, (f"TCP connect to {dest[0]}:{dest[1]} from {ip} timed out after "
                       f"{PROBE_TIMEOUT:g}s - the address is on a NIC but nothing routes it")
    except OSError as e:
        return False, f"TCP connect to {dest[0]}:{dest[1]} from {ip} failed: {e}"
    finally:
        s.close()


def _install_timeouts():
    """Bound every Kotak HTTP call. Idempotent; independent of any pin."""
    import requests.sessions as _rs
    if getattr(_rs.Session.request, "_dualmom_bounded", False):
        return
    _orig_request = _rs.Session.request

    def _bounded_request(self, method, url, *a, **kw):
        if kw.get("timeout") is None and PIN_SUFFIX in str(url).lower():
            kw["timeout"] = KOTAK_TIMEOUT
        return _orig_request(self, method, url, *a, **kw)

    _bounded_request._dualmom_bounded = True
    _rs.Session.request = _bounded_request


def _install_pin(ip: str):
    """Route *.kotaksecurities.com out of `ip`. Caller has already verified it."""
    import urllib3.util.connection as u3c
    original = u3c.create_connection

    def pinned_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, (tuple, list)) else None
        if not _host_is_kotak(host):
            return original(address, *args, **kwargs)
        # source_address is the 3rd positional arg in urllib3 2.x
        if len(args) >= 2:
            args = list(args)
            args[1] = (ip, 0)
            args = tuple(args)
        else:
            kwargs["source_address"] = (ip, 0)
        sock = original(address, *args, **kwargs)
        try:
            local = sock.getsockname()[0]
            _seen.append((host, local))
            del _seen[:-50]
            if local != ip:
                sock.close()
                raise OSError(f"Kotak connection to {host} left from {local}, "
                              f"not the pinned {ip}")
        except OSError:
            raise
        except Exception:
            pass
        return sock

    pinned_create_connection._dualmom_pinned = True
    u3c.create_connection = pinned_create_connection


def install(ip: str = None) -> str:
    """Bound every Kotak call, and pin the source IP if one is asked for AND works.

    Returns the address Kotak traffic will leave from: the pinned IP, or "default"
    when no pin is in effect. Never raises for an unreachable pin — a dead pin
    degrades to the default IP (which the account accepts), because the alternative
    is a total outage. Idempotent.
    """
    global _installed_ip, _installed, _pin_skipped
    ip = (ip if ip is not None else SOURCE_IP).strip()
    want_pin = ip.lower() not in _NO_PIN

    with _lock:
        _install_timeouts()

        if not want_pin:
            _installed = True
            return _installed_ip or "default"

        if _installed_ip == ip:
            return ip
        if _installed_ip is not None:
            raise RuntimeError(f"Kotak source IP already pinned to {_installed_ip}; "
                               f"refusing to re-pin to {ip}")

        # A pin is process-wide, so it must never be installed alongside the
        # strangle's Kotak client - that would send its orders from the wrong IP.
        # This one still fails hard: it is a correctness risk, not a reachability one.
        clash = _strangle_loaded()
        if clash:
            raise RuntimeError(
                "Refusing to pin Kotak traffic to Kotak Rohit's IP in a process that "
                f"also holds the strangle's Kotak client ({', '.join(clash)}). The pin "
                "is process-wide and would send the strangle's orders from the wrong "
                "IP. Run DualMom in its own service.")

        if not _ip_is_local(ip):
            _pin_skipped = f"{ip} is not configured on this machine"
            _installed = True
            return "default"

        ok, detail = _pin_reaches_kotak(ip)
        if not ok:
            # The failure mode this module exists to prevent is silent breakage, so
            # say it loudly - but keep DualMom working from the default IP.
            _pin_skipped = detail
            print(f"  [dualmom] WARNING: source-IP pin {ip} SKIPPED - {detail}. "
                  f"Kotak traffic will leave from the default IP instead.", flush=True)
            _installed = True
            return "default"

        _install_pin(ip)
        _installed_ip = ip
        _pin_skipped = None
        _installed = True
        return ip


def installed_ip():
    return _installed_ip


def status() -> dict:
    """What the pin is actually doing — surfaced by /status so a silent
    fallback to the default IP is visible instead of being guessed at."""
    return {
        "requested": SOURCE_IP or None,
        "pinned": _installed_ip,
        "effective": _installed_ip or "default",
        "timeouts_installed": _installed,
        "pin_skipped_because": _pin_skipped,
        "recent": list(_seen[-5:]),
    }


def recent_connections() -> list:
    """[(host, local_ip)] — proof of which address Kotak traffic actually used."""
    return list(_seen)
