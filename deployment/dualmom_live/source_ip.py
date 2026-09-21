"""
Pin Kotak Rohit's API traffic to its own whitelisted source IP.

WHY
    Kotak binds a whitelisted IP to ONE client (UCC). The VPS's main IP,
    144.79.166.103, already belongs to the strangle's Kotak account, so Kotak Rohit
    (UCC 15P56) was given a secondary IP, 103.49.131.3. It is configured on the NIC
    with SkipAsSource=True, so Windows never uses it on its own — every connection
    leaves from 144.79.166.103 unless a program explicitly binds to .3. This module
    does that binding.

HOW
    neo_api_client makes plain module-level requests.get/post calls (no Session),
    so there is no per-client object to configure. Instead we wrap urllib3's
    create_connection — the single point every requests call goes through — and
    add source_address ONLY when the destination host is *.kotaksecurities.com.

    Everything else in the same process is untouched on purpose:
      * the Fyers data refresh (api-t1.fyers.in) keeps leaving from 144.79.166.103
      * localhost calls (the dashboard proxy -> :8010) must never be bound to a
        public address, or they fail outright

SCOPE — this is a PROCESS-wide patch
    If this process also ran the strangle's Kotak client, that client talks to the
    same *.kotaksecurities.com hosts and would get pinned too — sending the
    strangle's orders from Kotak Rohit's IP. install() therefore REFUSES to run in
    any process that has the strangle's Kotak modules loaded. Today only the
    DualMom service (and scripts run by hand) log in as Kotak Rohit, and the
    dashboard reaches DualMom through a proxy, so they never share a process.

FAIL CLOSED
    If 103.49.131.3 is not bound on this machine, install() raises instead of
    quietly letting Kotak Rohit log in from the strangle's IP.
"""

import os
import socket
import sys
import threading

SOURCE_IP = os.getenv("KOTAK_DM_SOURCE_IP", "103.49.131.3").strip()
PIN_SUFFIX = "kotaksecurities.com"
KOTAK_TIMEOUT = (10, 30)   # (connect, read) seconds for every Kotak HTTP call

# modules that belong to the STRANGLE's Kotak leg — never share a process with them
_STRANGLE_MARKERS = ("live.kotak_auth", "kotak_executor", "kotak_controller",
                     "live_tick_engine")

_lock = threading.Lock()
_installed_ip = None
_seen = []          # (host, local_ip) of recent pinned connections, for verification


def _host_is_kotak(host) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    return h == PIN_SUFFIX or h.endswith("." + PIN_SUFFIX)


def _strangle_loaded() -> list:
    return sorted(m for m in list(sys.modules)
                  if any(m.endswith(x) or f".{x}" in m for x in _STRANGLE_MARKERS)
                  and "dualmom" not in m)


def _ip_is_local(ip: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


def install(ip: str = None) -> str:
    """Pin *.kotaksecurities.com connections in THIS process to `ip`. Idempotent."""
    global _installed_ip
    ip = (ip or SOURCE_IP).strip()
    with _lock:
        if _installed_ip == ip:
            return ip
        if _installed_ip is not None:
            raise RuntimeError(f"Kotak source IP already pinned to {_installed_ip}; "
                               f"refusing to re-pin to {ip}")

        clash = _strangle_loaded()
        if clash:
            raise RuntimeError(
                "Refusing to pin Kotak traffic to Kotak Rohit's IP in a process that "
                f"also holds the strangle's Kotak client ({', '.join(clash)}). The pin "
                "is process-wide and would send the strangle's orders from the wrong "
                "IP. Run DualMom in its own service.")

        if not _ip_is_local(ip):
            raise RuntimeError(
                f"Kotak Rohit's source IP {ip} is not configured on this machine. "
                "Refusing to log in from the default IP, which is whitelisted for the "
                "strangle's account.")

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

        # The SDK calls requests with no timeout. On 2026-09-21 a limits() call sat
        # in the TLS handshake indefinitely and every dashboard request queued
        # behind the shared-session lock. Bound every Kotak call.
        import requests.sessions as _rs
        _orig_request = _rs.Session.request

        def _bounded_request(self, method, url, *a, **kw):
            if kw.get("timeout") is None and PIN_SUFFIX in str(url).lower():
                kw["timeout"] = KOTAK_TIMEOUT
            return _orig_request(self, method, url, *a, **kw)

        _rs.Session.request = _bounded_request
        _installed_ip = ip
        return ip


def installed_ip():
    return _installed_ip


def recent_connections() -> list:
    """[(host, local_ip)] — proof of which address Kotak traffic actually used."""
    return list(_seen)
