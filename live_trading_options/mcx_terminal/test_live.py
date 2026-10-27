"""
mcx_terminal/test_live.py — offline tests for the LIVE book with a FAKE Kotak client (no network,
no login, no real order can be sent from here).

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_live
"""
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal import live as L

HERE = Path(__file__).resolve().parent
PASS = FAIL = 0
L.VERDICT_WAIT_S = 0.3


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name} {info}")


class FakeKotak:
    """Kotak order API look-alike. Orders sit 'open' until the test fills / rejects them."""

    def __init__(self):
        self.rows = {}           # oid -> order_report row
        self.sent = []           # every place_order kwargs
        self.mods, self.cancels = [], []
        self.n = 1000
        self.refuse = None       # place_order reply error text
        self.instant = None      # "complete" / "rejected": the exchange answers at once
        self.manual = []         # rows of orders placed by hand on the same account

    def place_order(self, **kw):
        self.sent.append(kw)
        if self.refuse:
            return {"stat": "Not_Ok", "errMsg": self.refuse, "stCode": 1009}
        self.n += 1
        oid = f"26102700{self.n}"
        row = {"nOrdNo": oid, "trdSym": kw["trading_symbol"], "trnsTp": kw["transaction_type"],
               "qty": int(kw["quantity"]), "fldQty": 0, "avgPrc": "0.00", "prc": kw["price"], "ordSt": "open",
               "prod": kw["product"], "tag": kw["tag"], "exSeg": kw["exchange_segment"]}
        if self.instant == "complete":
            row.update(fldQty=row["qty"], avgPrc=kw["price"], ordSt="complete")
        elif self.instant == "rejected":
            row.update(ordSt="rejected", rejRsn="RMS:Margin Exceeds")
        self.rows[oid] = row
        return {"nOrdNo": oid, "stat": "Ok", "stCode": 200}

    def order_report(self):
        rows = list(self.rows.values()) + self.manual
        return {"data": [dict(r) for r in rows]} if rows else {"stat": "Not_Ok", "errMsg": "No Data"}

    def cancel_order(self, order_id, **kw):
        self.cancels.append(order_id)
        self.rows[order_id]["ordSt"] = "cancelled"
        return {"result": order_id, "stat": "Ok", "stCode": 200}

    def modify_order(self, **kw):
        self.mods.append(kw)
        self.rows[kw["order_id"]]["prc"] = kw["price"]
        return {"nOrdNo": kw["order_id"], "stat": "Ok", "stCode": 200}

    def fill(self, oid, qty, avg):
        r = self.rows[oid]
        r["fldQty"], r["avgPrc"] = qty, f"{avg:.2f}"
        r["ordSt"] = "complete" if qty >= r["qty"] else "open"


def _live(name, quotes, armed=True, fake=None, path=None):
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    u = I.parse(text, today="2026-10-19")
    path = path or Path(tempfile.gettempdir()) / f"mcx_live_test_{name}.json"
    if fake is None:
        path.unlink(missing_ok=True)
    fake = fake or FakeKotak()
    flag = {"armed": armed}
    b = L.LiveBook(u, lambda tok: quotes.get(tok, {}), sessions=None, armed=lambda: flag["armed"],
                   path=path, client=fake, start_poller=False)
    return u, b, fake, flag, path


def test_units_tag_limit_and_fill():
    q = {}
    u, b, k, flag, path = _live("basic", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100, "depth": None}
    r = b.place(ce.token, "S", 100, "MKT")                    # 1 lot SELL
    s = k.sent[-1]
    check("qty sent in UNITS (1 lot = 100)", s["quantity"] == "100", s)
    check("limit order on mcx_fo", s["order_type"] == "L" and s["exchange_segment"] == "mcx_fo", s)
    check("sell priced bid-1% on tick", s["price"] == "494.00", s["price"])
    check("kotak side code", s["transaction_type"] == "S", s)
    check("own tag prefix", s["tag"].startswith(L.TAG) and len(s["tag"]) <= 13, s["tag"])
    check("no fill invented", r["ok"] and r["state"] == "waiting" and not b.pos.get(ce.token, {}).get("net"), r)
    o = b.orders[-1]
    k.fill(o["broker_id"], 100, 498.6)
    b.poll()
    check("fill from Kotak", o["status"] == "COMPLETE" and o["filled"] == 100 and abs(o["avg"] - 498.6) < 1e-9, o)
    check("short 1 lot", b.pos[ce.token]["net"] == -100 and abs(b.pos[ce.token]["avg"] - 498.6) < 1e-9, b.pos)
    b.poll()
    check("fill counted once", b.pos[ce.token]["net"] == -100, b.pos)
    path.unlink(missing_ok=True)


def test_instant_fill_green_and_reject_red():
    q = {}
    u, b, k, flag, path = _live("verdict", q)
    pe = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["PE"]
    q[pe.token] = {"ltp": 300.0, "bid": 299.0, "ask": 300.0, "bq": 100, "aq": 100, "depth": None}
    k.instant = "complete"
    r = b.place(pe.token, "B", 200, "MKT")
    check("executed pop-up", r["ok"] and r["state"] == "filled" and "BOUGHT 2 lots" in r["msg"], r)
    k.instant = "rejected"
    r = b.place(pe.token, "B", 100, "MKT")
    check("exchange reject -> red", not r["ok"] and r["state"] == "rejected" and "Margin" in r["msg"], r)
    k.instant, k.refuse = None, "RMS:Insufficient funds"
    r = b.place(pe.token, "B", 100, "MKT")
    check("place refused -> red", not r["ok"] and "Insufficient" in r["msg"], r)
    check("position only from real fills", b.pos[pe.token]["net"] == 200, b.pos)
    path.unlink(missing_ok=True)


def test_freeze_slices_partial_and_cancel():
    q = {}
    u, b, k, flag, path = _live("slices", q)
    s = u.products["SILVERM"]
    exp = s.expiries()[0]
    leg = next(iter(s.chains[exp].values()))["CE"]
    q[leg.token] = {"ltp": 1500.0, "bid": 1499.0, "ask": 1500.0, "bq": 10, "aq": 10, "depth": None}
    r = b.place(leg.token, "B", 130 * leg.lot, "MKT")          # 130 lots, freeze 120 lots
    sent = [int(x["quantity"]) for x in k.sent]
    check("freeze split 600 + 50", sent == [600, 50], sent)
    check("distinct tags", len({x["tag"] for x in k.sent}) == 2)
    o1, o2 = b.orders[-2], b.orders[-1]
    k.fill(o1["broker_id"], 300, 1500.0)
    b.poll()
    check("partial stays open", o1["status"] == "OPEN" and o1["filled"] == 300, o1)
    k.fill(o1["broker_id"], 600, 1500.5)                       # avg of all 600 = 1500.5 -> next 300 at 1501
    b.poll()
    check("second fill priced from new avg", abs(b.pos[leg.token]["avg"] - 1500.5) < 1e-9, b.pos[leg.token])
    r = b.cancel(o2["id"])
    b.poll()
    check("cancel goes to Kotak", r["ok"] and k.cancels == [o2["broker_id"]] and o2["status"] == "CANCELLED", (r, o2))
    check("cap still applies", not b.place(leg.token, "B", 301 * leg.lot, "MKT")["ok"])
    path.unlink(missing_ok=True)


def test_not_armed_blocks_new_risk_allows_exit():
    q = {}
    u, b, k, flag, path = _live("arm", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100, "depth": None}
    k.instant = "complete"
    b.place(ce.token, "S", 200, "MKT")
    flag["armed"] = False
    n = len(k.sent)
    r = b.place(ce.token, "S", 100, "MKT")
    check("disarmed: new risk refused", not r["ok"] and "not armed" in r["msg"] and len(k.sent) == n, r)
    r = b.place(ce.token, "B", 300, "MKT")
    check("disarmed: bigger-than-position buy refused", not r["ok"], r)
    b.settings = lambda: {
        "entry_ref": "touch", "entry_buffer": 1, "entry_unit": "%", "exit_ref": "touch", "exit_buffer": 1,
        "exit_unit": "%", "reprice": False, "market_after_s": 0, "product": "MIS", "session": "AUTO"}
    r = b.exit_all()
    check("disarmed: exit all still works", r["ok"] and b.pos[ce.token]["net"] == 0, (r, b.pos))
    check("exit in the position's product (NRML), not the MIS setting", k.sent[-1]["product"] == "NRML", k.sent[-1])
    path.unlink(missing_ok=True)


def test_manual_trades_ignored_and_crash_recovery():
    q = {}
    u, b, k, flag, path = _live("recover", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100, "depth": None}
    k.manual = [{"nOrdNo": "999", "trdSym": ce.symbol, "trnsTp": "B", "qty": 500, "fldQty": 500, "avgPrc": "480",
                 "ordSt": "complete", "prod": "NRML", "tag": ""}]
    b.poll()
    check("manual trade never counted", not b.pos.get(ce.token, {}).get("net") and not b.orders, b.pos)
    # an order of ours that reached Kotak but not our file (crash right after sending)
    k.rows["777"] = {"nOrdNo": "777", "trdSym": ce.symbol, "trnsTp": "S", "qty": 100, "fldQty": 100,
                     "avgPrc": "501.00", "prc": "495.00", "ordSt": "complete", "prod": "NRML", "tag": L.TAG + "101010001"}
    b.poll()
    check("own lost order adopted", b.pos[ce.token]["net"] == -100 and b.orders[-1]["broker_id"] == "777", b.orders)
    # reply lost: our file has the order (no id), Kotak has it -> matched by tag
    o = {"id": 50, "ts": "", "t0": time.time(), "token": ce.token, "symbol": ce.symbol, "name": ce.name, "side": "B",
         "qty": 100, "lot": 100, "kind": "MKT", "limit": 505.0, "filled": 0, "avg": 0.0, "status": "SENDING",
         "reason": "", "source": "", "role": "exit", "product": "NRML", "session": "AUTO", "t_rep": 0, "slice": "",
         "live": True, "tag": L.TAG + "202020002", "broker_id": None}
    b.orders.append(o)
    k.rows["888"] = {"nOrdNo": "888", "trdSym": ce.symbol, "trnsTp": "B", "qty": 100, "fldQty": 100, "avgPrc": "502.00",
                     "prc": "505.00", "ordSt": "complete", "prod": "NRML", "tag": o["tag"]}
    b.poll()
    check("reply-lost order matched by tag", o["broker_id"] == "888" and o["status"] == "COMPLETE", o)
    check("flat after both", b.pos[ce.token]["net"] == 0, b.pos)
    # restart: the book reloads from disk and finds nothing new to double count
    _, b2, _, _, _ = _live("recover", q, fake=k, path=path)
    b2.poll()
    check("restart keeps the book, no double count", b2.pos[ce.token]["net"] == 0 and len(b2.orders) == len(b.orders),
          (b2.pos, len(b2.orders)))
    path.unlink(missing_ok=True)


def test_reprice_uses_modify():
    q = {}
    u, b, k, flag, path = _live("modify", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100, "depth": None}
    b.settings = lambda: {"entry_ref": "touch", "entry_buffer": 0, "entry_unit": "pts", "exit_ref": "touch",
                          "exit_buffer": 0, "exit_unit": "pts", "reprice": True, "market_after_s": 0,
                          "product": "NRML", "session": "AUTO"}
    b.place(ce.token, "S", 100, "MKT")
    o = b.orders[-1]
    check("priced at the bid", o["limit"] == 499.0, o["limit"])
    q[ce.token] = {"ltp": 497.0, "bid": 497.0, "ask": 497.5, "bq": 100, "aq": 100, "depth": None}
    o["t_rep"] = 0
    b.housekeep()
    m = k.mods[-1] if k.mods else {}
    check("modify sent at the new bid, full qty in units", m.get("price") == "497.00" and m.get("quantity") == "100", m)
    b.poll()
    check("limit follows Kotak", o["limit"] == 497.0, o["limit"])
    path.unlink(missing_ok=True)


def test_access_sets_user():
    import asyncio
    from live_trading_options.mcx_terminal.access import AccessGuard
    seen = {}

    async def inner(scope, receive, send):
        seen["u"] = scope.get("mcx_user")

    g = AccessGuard(inner, team="t", aud="a", emails="")
    asyncio.run(g({"type": "websocket", "path": "/ws", "headers": [(b"host", b"localhost:47680")]}, None, None))
    check("local user", seen.get("u") == "local", seen)


def test_engine_arm_owner_only():
    import os
    from live_trading_options.mcx_terminal import server as SV
    os.environ["MCX_OWNER_EMAILS"] = "prakhar4sapa@gmail.com"     # test the rule, not the VPS's own list
    e = SV.Engine()
    e.live = type("LB", (), {"broker": {"ok": True}, "open_positions": lambda self: 0})()
    e.rec = None
    e._arm_path = lambda: Path(tempfile.gettempdir()) / "mcx_live_test_arm.json"
    r = e.set_mode("LIVE", "khandekamohitmohit@gmail.com", "LIVE")
    check("brother cannot arm", not r["ok"] and e.mode == "PAPER", r)
    r = e.set_mode("LIVE", "prakhar4sapa@gmail.com", "yes")
    check("needs the typed word", not r["ok"] and e.mode == "PAPER", r)
    r = e.set_mode("LIVE", "prakhar4sapa@gmail.com", "live")
    check("owner arms", r["ok"] and e.mode == "LIVE", r)
    r = e.set_mode("PAPER", "khandekamohitmohit@gmail.com")
    check("anyone may DISarm", r["ok"] and e.mode == "PAPER", r)
    e._arm_path().unlink(missing_ok=True)


if __name__ == "__main__":
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        fn()
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
