"""
Tests for the Kotak Rohit source-IP pin. No network.

The real IP (103.49.131.3) only exists on the VPS, so the pin is exercised here
against 127.0.0.1 with urllib3's create_connection replaced by a recorder.

    python -m deployment.dualmom_live.tests.test_source_ip
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

import urllib3.util.connection as u3c

from deployment.dualmom_live import source_ip as SIP

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


class FakeSock:
    def __init__(self, local):
        self.local, self.closed = local, False

    def getsockname(self):
        return (self.local, 50000)

    def close(self):
        self.closed = True


calls = []
lie_about_source = {"on": False}


def fake_create_connection(address, timeout=None, source_address=None, socket_options=None):
    """Stands in for urllib3's real one: records what it was asked to bind."""
    calls.append({"host": address[0], "source": source_address})
    local = source_address[0] if source_address else "144.79.166.103"
    if lie_about_source["on"]:
        local = "144.79.166.103"
    return FakeSock(local)


print("\n=== 1. host matching ===")
check("mnapi.kotaksecurities.com is Kotak", SIP._host_is_kotak("mnapi.kotaksecurities.com"))
check("gw-napi.kotaksecurities.com is Kotak", SIP._host_is_kotak("gw-napi.kotaksecurities.com"))
check("bare kotaksecurities.com is Kotak", SIP._host_is_kotak("kotaksecurities.com"))
check("api-t1.fyers.in is NOT", not SIP._host_is_kotak("api-t1.fyers.in"))
check("127.0.0.1 is NOT", not SIP._host_is_kotak("127.0.0.1"))
check("lookalike evil-kotaksecurities.com is NOT",
      not SIP._host_is_kotak("evil-kotaksecurities.com"))

print("\n=== 2. refuses to share a process with the strangle's Kotak client ===")
sys.modules["live.kotak_auth"] = types.ModuleType("live.kotak_auth")
try:
    SIP.install("127.0.0.1")
    check("install refused when strangle module loaded", False, "it installed")
except RuntimeError as e:
    check("install refused when strangle module loaded", "strangle" in str(e))
del sys.modules["live.kotak_auth"]
check("DualMom's own kotak_auth_dm is not mistaken for the strangle's",
      not [m for m in SIP._strangle_loaded() if "dualmom" in m])

print("\n=== 3. fails closed if the IP is not on this machine ===")
try:
    SIP.install("198.51.100.77")       # TEST-NET-2, never local
    check("unbound IP refused", False, "it installed")
except RuntimeError as e:
    check("unbound IP refused", "not configured" in str(e))
check("nothing pinned after refusal", SIP.installed_ip() is None)

print("\n=== 4. routing: only Kotak hosts get the pinned source ===")
u3c.create_connection = fake_create_connection
SIP.install("127.0.0.1")
check("installed", SIP.installed_ip() == "127.0.0.1")

calls.clear()
u3c.create_connection(("mnapi.kotaksecurities.com", 443), 10, source_address=None,
                      socket_options=None)
check("Kotak host bound to pinned IP", calls[-1]["source"] == ("127.0.0.1", 0), calls[-1])

u3c.create_connection(("api-t1.fyers.in", 443), 10, source_address=None,
                      socket_options=None)
check("Fyers host left on default route", calls[-1]["source"] is None, calls[-1])

u3c.create_connection(("127.0.0.1", 8010), 10, source_address=None, socket_options=None)
check("localhost proxy call left alone", calls[-1]["source"] is None, calls[-1])

u3c.create_connection(("cnapi.kotaksecurities.com", 443), 10, ("0.0.0.0", 0), None)
check("positional source_address also overridden", calls[-1]["source"] == ("127.0.0.1", 0),
      calls[-1])

check("recent_connections records Kotak hosts only",
      all(SIP._host_is_kotak(h) for h, _ in SIP.recent_connections()))

print("\n=== 5. a connection that did NOT leave from the pinned IP is refused ===")
lie_about_source["on"] = True
try:
    u3c.create_connection(("mnapi.kotaksecurities.com", 443), 10, source_address=None,
                          socket_options=None)
    check("wrong local address raises", False, "it returned a socket")
except OSError as e:
    check("wrong local address raises", "not the pinned" in str(e))
lie_about_source["on"] = False

print("\n=== 6. idempotent, and never silently re-pins ===")
check("same IP again is a no-op", SIP.install("127.0.0.1") == "127.0.0.1")
try:
    SIP.install("127.0.0.2")
    check("different IP refused", False, "re-pinned")
except RuntimeError:
    check("different IP refused", True)

print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    FAILED: {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
