"""
tools/vps/kotak_session_test.py — does a SECOND Kotak login (same API key) kill the FIRST?
==========================================================================================

Why: Kotak gives one API app (consumer key) per account. The Strategy Builder wants its
own Kotak session next to the strangle engine's. If a second login logs the first one
out, the builder must route its Kotak orders through the engine instead.

What it does (READ-ONLY — places NO order; account via --account bhaiya|rohit, default bhaiya):
    1. session A logs in, reads the order book + positions
    2. session B logs in with the SAME key, reads
    3. session A reads again (immediately, +30 s, +90 s)  <- the actual question
    4. session B reads again
Prints OK / FAILED per read and a verdict. Never prints tokens or credentials.

Refuses to run during market hours or while any Kotak-trading engine is running, so it
can never knock a live session off.

    .venv\\Scripts\\python.exe tools\\vps\\kotak_session_test.py
"""
import sys
import time
import datetime as dt
import subprocess
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "live_trading_options" / "strangle_strategy"))
from live.kotak_auth import login          # noqa: E402  (loads deployment/.env)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def guard():
    n = dt.datetime.now(IST)
    if n.weekday() < 5 and dt.time(9, 0) <= n.time() <= dt.time(15, 35):
        sys.exit(f"REFUSED: market hours ({n:%a %H:%M} IST). Run after 15:35 or at the weekend.")
    ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                         "Select-Object -ExpandProperty CommandLine"],
                        capture_output=True, text=True, timeout=30).stdout
    busy = [l.strip() for l in ps.splitlines()
            if "kotak_session_test" not in l and any(k in l for k in ("live_tick_engine", "kotak", "delta_neutral", "dualmom_service"))]
    if busy:
        sys.exit("REFUSED: a Kotak-capable engine is running:\n  " + "\n  ".join(busy))
    print(f"guard ok — {n:%a %d-%b %H:%M} IST, no Kotak engine running\n")


def probe(client, label):
    """Three read-only calls. A logged-out session answers with an error dict / raises.
    stCode 5203 'No Data' is an EMPTY book on a LIVE session (seen 16-Oct evening), not a failure."""
    res = []
    for name, fn in (("limits", lambda: client.limits(segment="ALL", exchange="ALL", product="ALL")),
                     ("order_report", lambda: client.order_report()),
                     ("positions", lambda: client.positions())):
        try:
            r = fn()
            empty = isinstance(r, dict) and str(r.get("stCode")) == "5203"
            bad = not empty and isinstance(r, dict) and (r.get("error") or r.get("Error Message")
                                           or str(r.get("stat", "")).lower() == "not_ok"
                                           or "invalid" in str(r).lower()[:300]
                                           or "session" in str(r).lower()[:300] and "expire" in str(r).lower()[:300])
            res.append((name, not bad, "" if not bad else str(r)[:160]))
        except Exception as e:
            res.append((name, False, f"{type(e).__name__}: {str(e)[:140]}"))
    ok = all(x[1] for x in res)
    print(f"  {label:<26} {'OK ' if ok else 'FAILED'}  " +
          "  ".join(f"{n}={'ok' if g else 'X'}" for n, g, _ in res))
    for n, g, why in res:
        if not g:
            print(f"      {n}: {why}")
    return ok


def main():
    acct = sys.argv[sys.argv.index("--account") + 1] if "--account" in sys.argv else "bhaiya"
    if acct not in ("bhaiya", "rohit"):
        sys.exit("--account must be bhaiya or rohit")
    kw = {"rohit": acct == "rohit"}
    guard()
    print(f"1. session A logs in (Kotak {acct})")
    A = login(verbose=False, **kw)
    a0 = probe(A, "A after own login")
    if not a0:
        sys.exit("\nA cannot even read after its own login — check the account/IP before testing sessions.")

    time.sleep(5)
    print("2. session B logs in — SAME consumer key")
    B = login(verbose=False, **kw)
    b0 = probe(B, "B after own login")

    print("3. is A still alive?")
    a1 = probe(A, "A right after B login")
    time.sleep(30)
    a2 = probe(A, "A +30 s")
    b1 = probe(B, "B +30 s")
    time.sleep(60)
    a3 = probe(A, "A +90 s")
    b2 = probe(B, "B +90 s")

    print("\nVERDICT")
    if a1 and a2 and a3 and b0 and b1 and b2:
        print("  BOTH sessions stay alive. The builder can hold its OWN Kotak session next to")
        print("  the strangle engine — no routing through the engine needed.")
    elif b0 and not (a1 and a2 and a3):
        print("  The SECOND login KILLED the first. The builder must NOT log into Kotak itself —")
        print("  its Kotak orders go through the engine that already holds the session.")
    else:
        print("  Mixed result — read the lines above before deciding.")


if __name__ == "__main__":
    main()
