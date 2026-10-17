"""Offline safety tests for orders.py with FAKE brokers (no network, no real orders).
Run:  .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.test_orders"""
import sys
import json
import tempfile
import threading
import time
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from live_trading_options.strategy_builder import orders as O

IST = O.IST
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class Acct:
    def __init__(self, aid, kind="kite"):
        self.id, self.name, self.kind = aid, aid.upper(), kind
        self.lock = threading.RLock()

    def ensure(self):
        return "client"

    def refresh_funds(self):
        pass


class Hub:
    def __init__(self):
        self.a = {"z": Acct("z", "kite"), "k": Acct("k", "kotak")}

    def get(self, i):
        return self.a[i]


class FakeAdapter:
    """Records every order. `reject` = symbols the exchange rejects; `nofill` = symbols that
    never fill (stay OPEN); `partial` = {symbol: max qty that ever fills per order}."""
    def __init__(self):
        self.sent, self.cancelled = [], []
        self.reject, self.nofill, self.partial = set(), set(), {}
        self.state = {}

    def price(self, c, k, side, qty, fallback, cushion):
        return 100.0 + (cushion if side == "B" else -cushion) * 0.05

    def place(self, c, k, side, qty, price, product, tag):
        if k["symbol"] in self.reject:
            raise RuntimeError("RMS: insufficient margin")
        oid = f"o{len(self.sent) + 1}"
        self.sent.append({"oid": oid, "sym": k["symbol"], "side": side, "qty": qty, "price": price, "tag": tag})
        if k["symbol"] in self.nofill:
            f = 0
        else:
            f = min(qty, self.partial.get(k["symbol"], qty))
        self.state[oid] = {"status": "COMPLETE" if f == qty else "OPEN", "filled": f, "avg": price}
        return oid, tag

    def status(self, c, oid):
        s = self.state[oid]
        return {"status": s["status"], "filled": s["filled"], "avg": s["avg"], "time": "10:00:00", "msg": None}

    def cancel(self, c, oid):
        self.cancelled.append(oid)
        if self.state[oid]["status"] == "OPEN":
            self.state[oid]["status"] = "CANCELLED"


def resolve(acct, leg):
    return {"symbol": f"{leg['index']}{int(leg['strike'])}{leg['type']}", "exchange": "NFO", "lot": 65 if leg["index"] == "NIFTY" else 20, "token": "t"}


QUOTES = {}


def quotes(legs):
    return {l["fyers_symbol"]: QUOTES.get(l["fyers_symbol"], {"ltp": 100.0}) for l in legs}


OPEN_T = dt.datetime(2026, 10, 19, 10, 0, tzinfo=IST)          # Monday 10:00 IST


def make(tmp, clock=lambda: OPEN_T):
    O.DEP_DIR = Path(tmp) / "deployments"
    O.CONTROL = Path(tmp) / "control.json"
    O.C.DATA = Path(tmp)
    ad = FakeAdapter()
    desk = O.Desk(Hub(), quotes, {"kite": ad, "kotak": ad}, resolve, clock=clock,
                  sleep=lambda s: None, fill_wait=0)
    return desk, ad


def wait_idle(desk, t=5):
    end = time.time() + t
    while desk._busy and time.time() < end:
        time.sleep(0.01)


def leg(side, k, typ, lots=1, idx="NIFTY"):
    return {"index": idx, "expiry": "2026-10-20", "strike": k, "type": typ, "side": side, "lots": lots,
            "price": 100.0, "symbol": f"NSE:X{k}{typ}"}


IRON_FLY = [leg("S", 23100, "CE"), leg("S", 23100, "PE"), leg("B", 23300, "CE"), leg("B", 22900, "PE")]


def req(legs=IRON_FLY, brokers=None, **kw):
    return {"name": "t", "legs": legs, "brokers": brokers or {"z": 1}, **kw}


with tempfile.TemporaryDirectory() as tmp:
    desk, ad = make(tmp)
    # 1. not armed -> refused
    try:
        desk.deploy(req()); check("refuses when not armed", False)
    except ValueError as e:
        check("refuses when not armed", "armed" in str(e).lower())
    desk.set_armed(True)

    # 2. closed market -> refused
    d2, _ = make(tmp, clock=lambda: dt.datetime(2026, 10, 17, 10, 0, tzinfo=IST))   # Saturday
    try:
        d2.deploy(req()); check("refuses on a closed market", False)
    except ValueError as e:
        check("refuses on a closed market", "closed" in str(e).lower())

    # 3. lot cap
    try:
        desk.deploy(req(brokers={"z": 60})); check("lot cap", False)
    except ValueError as e:
        check("lot cap", "cap" in str(e))

    # 4. happy path: buys first, then sells; book flat after exit with shorts first
    ids = desk.deploy(req())
    wait_idle(desk)
    d = desk.deps[ids[0]]
    sides = [o["side"] for o in ad.sent]
    check("entry: both BUY legs before any SELL", sides == ["B", "B", "S", "S"])
    check("entry: status open", d["status"] == "open")
    check("entry: tag on every order", all(o["tag"] == d["tag"] for o in ad.sent))
    v = desk.view(d)
    check("book: net -65 on shorts, +65 on longs", [l["net"] for l in v["legs"]] == [-65, -65, 65, 65])
    n0 = len(ad.sent)
    desk.square_off(d["id"]); wait_idle(desk)
    ex = ad.sent[n0:]
    check("exit: shorts bought back BEFORE hedges sold", [o["side"] for o in ex] == ["B", "B", "S", "S"])
    check("exit: flat + closed", d["status"] == "closed" and desk.view(d)["open_qty"] == 0)

    # 5. hedge rejected -> NO sell sent
    ad.sent.clear(); ad.reject = {"NIFTY23300CE"}
    ids = desk.deploy(req()); wait_idle(desk)
    d = desk.deps[ids[0]]
    check("hedge rejected: no SELL order sent", not any(o["side"] == "S" for o in ad.sent))
    check("first hedge rejected: status failed (nothing filled)", d["status"] == "failed")
    ad.sent.clear(); ad.reject = {"NIFTY22900PE"}
    ids = desk.deploy(req()); wait_idle(desk)
    d = desk.deps[ids[0]]
    check("second hedge rejected: no SELL, status partial", not any(o["side"] == "S" for o in ad.sent)
          and d["status"] == "partial")
    ad.reject = set()

    # 6. hedge never fills -> chased 4 rounds, cancelled each time, no sell
    ad.sent.clear(); ad.cancelled.clear(); ad.nofill = {"NIFTY22900PE"}
    ids = desk.deploy(req()); wait_idle(desk)
    pe = [o for o in ad.sent if o["sym"] == "NIFTY22900PE"]
    check("unfilled hedge: re-priced 4 times, wider each time", len(pe) == 4 and pe[-1]["price"] > pe[0]["price"])
    check("unfilled hedge: every attempt cancelled", all(o["oid"] in ad.cancelled for o in pe))
    check("unfilled hedge: no SELL", not any(o["side"] == "S" for o in ad.sent))
    ad.nofill = set()

    # 7. slicing: 45 lots NIFTY -> 20 + 20 + 5
    ad.sent.clear()
    ids = desk.deploy(req(legs=[leg("B", 23000, "CE", 45)])); wait_idle(desk)
    check("slices 45 lots as 1300+1300+325", [o["qty"] for o in ad.sent] == [1300, 1300, 325])

    # 8. partial fill on a short is chased for the remainder
    ad.sent.clear(); ad.partial = {"NIFTY23100CE": 30}
    ids = desk.deploy(req(legs=[leg("S", 23100, "CE")])); wait_idle(desk)
    d = desk.deps[ids[0]]
    ce = [o for o in ad.sent if o["sym"] == "NIFTY23100CE"]
    check("partial fills chased until done (30+30+5)", [o["qty"] for o in ce][:3] == [65, 35, 5])
    check("book after partials = -65", desk.view(d)["legs"][0]["net"] == -65)
    ad.partial = {}

    # 9. stop-loss on MTM
    ad.sent.clear()
    ids = desk.deploy(req(legs=[leg("S", 23150, "CE")], sl=1000)); wait_idle(desk)
    d = desk.deps[ids[0]]
    QUOTES["NSE:X23150CE"] = {"ltp": 100.0 + 2000 / 65}            # loss ~2000 on a 65 short
    desk.tick(); wait_idle(desk)
    check("SL: squared off when MTM <= -SL", d["status"] == "closed" and "stop-loss" in (d["exit_reason"] or ""))
    QUOTES.clear()

    # 10. target
    ids = desk.deploy(req(legs=[leg("S", 23200, "CE")], target=500)); wait_idle(desk)
    d = desk.deps[ids[0]]
    QUOTES["NSE:X23200CE"] = {"ltp": 90.0}
    desk.tick(); wait_idle(desk)
    check("target: squared off when MTM >= target", d["status"] == "closed" and "target" in (d["exit_reason"] or ""))
    QUOTES.clear()

    # 10b. missing price -> MTM unknown -> SL must NOT fire on a partial number
    ids = desk.deploy(req(legs=[leg("S", 23400, "CE"), leg("B", 23600, "CE")], sl=10)); wait_idle(desk)
    d = desk.deps[ids[0]]
    QUOTES["NSE:X23400CE"] = {"ltp": None}
    QUOTES["NSE:X23600CE"] = {"ltp": 50.0}                         # alone this leg is -3250 -> would "hit" SL 10
    check("missing price -> MTM is None", desk.view(d)["mtm"] is None)
    desk.tick(); wait_idle(desk)
    check("missing price -> SL not fired", d["status"] == "open")
    QUOTES.clear()
    desk.square_off(d["id"]); wait_idle(desk)

    # 11. two brokers -> two independent deployments
    ad.sent.clear()
    ids = desk.deploy(req(legs=[leg("B", 23000, "PE")], brokers={"z": 1, "k": 2})); wait_idle(desk)
    check("two brokers -> two deployments", len(ids) == 2 and {desk.deps[i]["broker"] for i in ids} == {"z", "k"})
    check("multiplier applied per broker", sorted(o["qty"] for o in ad.sent) == [65, 130])

    # 12. restart mid-flight -> interrupted, never resumed
    d = desk.deps[ids[0]]
    d["status"] = "placing"; desk._save(d)
    desk2 = O.Desk(Hub(), quotes, {"kite": ad, "kotak": ad}, resolve, clock=lambda: OPEN_T, sleep=lambda s: None, fill_wait=0)
    check("restart: in-flight deployment marked interrupted", desk2.deps[d["id"]]["status"] == "interrupted")

    # 13. exits work while DISARMED; entries do not
    desk.set_armed(False)
    ids_open = [i for i, x in desk.deps.items() if x["status"] == "open"]
    n = desk.square_off_all(); wait_idle(desk)
    check("kill switch works while disarmed", n >= 1 and all(desk.deps[i]["status"] == "closed" for i in ids_open))

    # 14. limit mode rests at the typed price and is refreshed by the monitor
    desk.set_armed(True)
    ad.sent.clear(); ad.nofill = {"NIFTY23250CE"}
    l = leg("S", 23250, "CE"); l["price"] = 123.45
    ids = desk.deploy(req(legs=[l], mode="limit")); wait_idle(desk)
    d = desk.deps[ids[0]]
    check("limit: one order at the typed price", len(ad.sent) == 1 and ad.sent[0]["price"] == 123.45)
    check("limit: deployment pending", d["status"] == "pending")
    ad.state[ad.sent[0]["oid"]].update(status="COMPLETE", filled=65)
    desk.tick()
    check("limit: monitor picks up the fill", d["status"] == "open" and desk.view(d)["legs"][0]["net"] == -65)
    ad.nofill = set()

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
