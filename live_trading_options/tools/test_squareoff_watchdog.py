"""test_squareoff_watchdog.py — the last-line-of-defence square-off.

OFFLINE, stub broker, places nothing.

The dangerous half of a watchdog is not failing to close — it is closing the
WRONG thing. These pin the own-book rules: it must act only on shorts our own
tagged orders opened, never on the user's manual positions, never in the wrong
product, and never for more quantity than actually exists.

    .venv\\Scripts\\python.exe live_trading_options/tools/test_squareoff_watchdog.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from squareoff_watchdog import own_net_shorts, broker_shorts, to_close

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"    ok   {name}")
    else:
        FAIL += 1
        print(f"    FAIL {name}: got {got!r}, want {want!r}")


def order(ts, side, qty, tag, status="COMPLETE", ex="NFO"):
    return {"tradingsymbol": ts, "transaction_type": side, "quantity": qty,
            "filled_quantity": qty, "tag": tag, "status": status, "exchange": ex}


def position(ts, qty, product="NRML", ex="NFO"):
    return {"tradingsymbol": ts, "quantity": qty, "product": product, "exchange": ex}


class Stub:
    def __init__(self, orders, positions):
        self._o, self._p = orders, positions

    def orders(self):
        return self._o

    def positions(self):
        return {"net": self._p}


print("\n  -- own_net_shorts: only OUR completed orders count --")

k = Stub([order("NIFTY24100CE", "SELL", 520, "dnstrangle")],
         [position("NIFTY24100CE", -520)])
check("a completed short is ours", own_net_shorts(k)["NIFTY24100CE"]["qty"], 520)

k = Stub([order("NIFTY24100CE", "SELL", 520, "dnstrangle"),
          order("NIFTY24100CE", "BUY", 520, "dnstrangle")], [])
check("sold then bought back -> not short", own_net_shorts(k), {})

k = Stub([order("NIFTY24100CE", "SELL", 520, "dnstrangle", status="REJECTED")], [])
check("a REJECTED order moves nothing", own_net_shorts(k), {})

k = Stub([order("NIFTY24100CE", "SELL", 520, "26090101100001783")],
         [position("NIFTY24100CE", -520)])
check("an untagged (manual) short is NOT ours", own_net_shorts(k), {})

k = Stub([order("NIFTY24150CE", "SELL", 520, "vwstrangle"),
          order("NIFTY23950PE", "SELL", 520, "vwstrangle")], [])
check("both VWAP legs are ours", sorted(own_net_shorts(k)), ["NIFTY23950PE", "NIFTY24150CE"])

k = Stub([order("NIFTY24100CE", "SELL", 520, "dnstrangle"),
          order("NIFTY24100CE", "SELL", 260, "dnstrangle"),
          order("NIFTY24100CE", "BUY", 260, "dnstrangle")], [])
check("partial cover nets correctly", own_net_shorts(k)["NIFTY24100CE"]["qty"], 520)


print("\n  -- to_close: ours AND actually open, capped by the broker --")

# the 2026-09-01 book: our shorts plus the user's manual longs and manual shorts
k = Stub(
    [order("NIFTY24150CE", "SELL", 520, "vwstrangle"),
     order("NIFTY23950PE", "SELL", 520, "vwstrangle"),
     order("NIFTY24100CE", "SELL", 520, "26090101100001783"),   # manual
     order("NIFTY24700CE", "BUY", 1040, "26090101100001126")],  # manual wing
    [position("NIFTY24150CE", -520, "MIS"),
     position("NIFTY23950PE", -520, "MIS"),
     position("NIFTY24100CE", -520, "NRML"),                    # manual short
     position("NIFTY24700CE", 1040, "NRML")])                   # manual long
jobs = {j["tradingsymbol"]: j for j in to_close(k)}
check("closes exactly our two legs", sorted(jobs), ["NIFTY23950PE", "NIFTY24150CE"])
check("leaves the manual short alone", "NIFTY24100CE" in jobs, False)
check("leaves the manual long alone", "NIFTY24700CE" in jobs, False)
check("uses the product the position is REALLY in", jobs["NIFTY24150CE"]["product"], "MIS")

# ours per our orders, but the broker says it is already gone
k = Stub([order("NIFTY24100CE", "SELL", 520, "dnstrangle")], [])
check("already flat at the broker -> nothing to do", to_close(k), [])

# our book says 520 but only 260 is actually open (a partial fill elsewhere)
k = Stub([order("NIFTY24100CE", "SELL", 520, "dnstrangle")],
         [position("NIFTY24100CE", -260)])
check("never buys back more than really exists", to_close(k)[0]["qty"], 260)

# broker is short MORE than we are — the excess is someone else's
k = Stub([order("NIFTY24100CE", "SELL", 260, "dnstrangle")],
         [position("NIFTY24100CE", -520)])
check("never buys back more than OUR book says", to_close(k)[0]["qty"], 260)

# a long position is never "closed"
k = Stub([order("NIFTY24700CE", "BUY", 1040, "dnstrangle")],
         [position("NIFTY24700CE", 1040)])
check("a LONG is never squared off by this", to_close(k), [])


print("\n  -- broker_shorts --")
k = Stub([], [position("A", -100, "NRML"), position("B", 50, "MIS"),
              position("C", 0, "NRML")])
b = broker_shorts(k)
check("only shorts are listed", sorted(b), ["A"])
check("quantity is positive magnitude", b["A"]["qty"], 100)
check("product is carried through", b["A"]["product"], "NRML")


print("\n  -- a broken broker must not throw --")


class Broken:
    def orders(self):
        raise RuntimeError("api down")

    def positions(self):
        raise RuntimeError("api down")


check("unreadable order book -> empty, no crash", own_net_shorts(Broken()), {})
check("unreadable positions -> empty, no crash", broker_shorts(Broken()), {})
check("to_close survives both", to_close(Broken()), [])

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
