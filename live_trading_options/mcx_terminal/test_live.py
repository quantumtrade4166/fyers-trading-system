"""
mcx_terminal/test_live.py — offline tests for the LIVE book with a FAKE Kotak client (no network,
no login, no real order can be sent from here).

Positions are a MIRROR of Kotak: the fake keeps a positions book built from every filled order
(the terminal's AND manual ones) plus overnight carry, like Kotak's positions() does.

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_live
"""
import datetime as dt
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal import live as L

HERE = Path(__file__).resolve().parent
PASS = FAIL = 0
L.VERDICT_WAIT_S = 0.3
L.EXIT_PUSH_S = 0.6
L.EXIT_VERIFY_S = 1.2
audit.DIR = Path(tempfile.gettempdir()) / "mcx_audit_test"
for _f in audit.DIR.glob("*.jsonl") if audit.DIR.exists() else []:
    _f.unlink()


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name} {info}")


class FakeKotak:
    """Kotak order + positions API look-alike. Orders sit 'open' until the test fills / rejects them."""

    def __init__(self):
        self.rows = {}           # oid -> order_report row (terminal's orders)
        self.manual = {}         # oid -> rows of orders placed by hand on the same account
        self.carry = {}          # (symbol, prod) -> [cfBuyQty, cfSellQty, cfBuyAmt, cfSellAmt]
        self.sent, self.mods, self.cancels = [], [], []
        self.n = 1000
        self.refuse = None       # place_order reply error text
        self.instant = None      # "complete" / "rejected": the exchange answers at once
        self.pos_fail = False    # positions() unreadable
        self.slow = {}           # symbol -> seconds place_order hangs
        self.fill_on_modify = False

    def place_order(self, **kw):
        if kw["trading_symbol"] in self.slow:
            time.sleep(self.slow[kw["trading_symbol"]])
        self.sent.append({**kw, "_t": time.time()})
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
        rows = list(self.rows.values()) + list(self.manual.values())
        return {"data": [dict(r) for r in rows]} if rows else {"stat": "Not_Ok", "errMsg": "No Data"}

    def positions(self):
        if self.pos_fail:
            return {"Error": "ReadTimeout"}
        agg = {}
        for (sym, prod), cf in self.carry.items():
            a = agg.setdefault((sym, prod), [0, 0, 0.0, 0.0, *cf])
        for r in list(self.rows.values()) + list(self.manual.values()):
            f = int(r["fldQty"])
            if not f:
                continue
            a = agg.setdefault((r["trdSym"], r["prod"]), [0, 0, 0.0, 0.0, 0, 0, 0.0, 0.0])
            if r["trnsTp"] == "B":
                a[0] += f
                a[2] += f * float(r["avgPrc"])
            else:
                a[1] += f
                a[3] += f * float(r["avgPrc"])
        if not agg:
            return {"stat": "Not_Ok", "stCode": 5203, "errMsg": "No Data"}
        return {"data": [{"trdSym": s, "prod": p, "exSeg": "mcx_fo", "flBuyQty": str(a[0]), "flSellQty": str(a[1]),
                          "buyAmt": f"{a[2]:.2f}", "sellAmt": f"{a[3]:.2f}", "cfBuyQty": str(a[4]),
                          "cfSellQty": str(a[5]), "cfBuyAmt": f"{a[6]:.2f}", "cfSellAmt": f"{a[7]:.2f}"}
                         for (s, p), a in agg.items()]}

    def cancel_order(self, order_id, **kw):
        self.cancels.append(order_id)
        (self.rows.get(order_id) or self.manual.get(order_id))["ordSt"] = "cancelled"
        return {"result": order_id, "stat": "Ok", "stCode": 200}

    def modify_order(self, **kw):
        self.mods.append(kw)
        r = self.rows[kw["order_id"]]
        r["prc"] = kw["price"]
        if self.fill_on_modify:
            r.update(fldQty=r["qty"], avgPrc=kw["price"], ordSt="complete")
        return {"nOrdNo": kw["order_id"], "stat": "Ok", "stCode": 200}

    def quotes(self, **kw):
        return [{"ltp": "100.0"}]

    def fill(self, oid, qty, avg):
        r = self.rows[oid]
        r["fldQty"], r["avgPrc"] = qty, f"{avg:.2f}"
        r["ordSt"] = "complete" if qty >= r["qty"] else "open"

    def hand_trade(self, sym, side, qty, px, prod="NRML", status="complete"):
        self.n += 1
        oid = f"MANUAL{self.n}"
        self.manual[oid] = {"nOrdNo": oid, "trdSym": sym, "trnsTp": side, "qty": qty,
                            "fldQty": qty if status == "complete" else 0, "avgPrc": f"{px:.2f}" if status == "complete" else "0",
                            "prc": f"{px:.2f}", "ordSt": status, "prod": prod, "tag": None, "exSeg": "mcx_fo"}
        return oid


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


def _q(ltp, spread=1.0):
    return {"ltp": ltp, "bid": ltp - spread / 2, "ask": ltp + spread / 2, "bq": 1000, "aq": 1000, "depth": None}


def test_units_tag_limit_and_fill_mirrored():
    q = {}
    u, b, k, flag, path = _live("basic", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100, "depth": None}
    r = b.place(ce.token, "S", 100, "MKT")                    # 1 lot SELL
    s = k.sent[-1]
    check("qty sent in UNITS (1 lot = 100)", s["quantity"] == "100", s)
    check("limit order on mcx_fo", s["order_type"] == "L" and s["exchange_segment"] == "mcx_fo", s)
    check("sell priced bid-1% on tick", s["price"] == "494.00", s["price"])
    check("own tag prefix", s["tag"].startswith(L.TAG) and len(s["tag"]) <= 13, s["tag"])
    check("no fill invented", r["ok"] and r["state"] == "waiting" and not b.pos.get(ce.token), r)
    o = b.orders[-1]
    k.fill(o["broker_id"], 100, 498.6)
    b.poll()
    b.sync_positions()
    check("fill from Kotak", o["status"] == "COMPLETE" and o["filled"] == 100, o)
    p = b.pos[ce.token]
    check("position = Kotak's: short 1 lot @ 498.6", p["net"] == -100 and abs(p["avg"] - 498.6) < 1e-9, p)
    b.poll(); b.sync_positions()
    check("not double counted", b.pos[ce.token]["net"] == -100, b.pos)
    path.unlink(missing_ok=True)


def test_instant_fill_green_and_reject_red():
    q = {}
    u, b, k, flag, path = _live("verdict", q)
    pe = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["PE"]
    q[pe.token] = _q(300.0)
    k.instant = "complete"
    r = b.place(pe.token, "B", 200, "MKT")
    check("executed pop-up", r["ok"] and r["state"] == "filled" and "BOUGHT 2 lots" in r["msg"], r)
    k.instant = "rejected"
    r = b.place(pe.token, "B", 100, "MKT")
    check("exchange reject -> red", not r["ok"] and r["state"] == "rejected" and "Margin" in r["msg"], r)
    k.instant, k.refuse = None, "RMS:Insufficient funds"
    r = b.place(pe.token, "B", 100, "MKT")
    check("place refused -> red", not r["ok"] and "Insufficient" in r["msg"], r)
    b.sync_positions()
    check("position only from real fills", b.pos[pe.token]["net"] == 200, b.pos)
    path.unlink(missing_ok=True)


def test_freeze_slices_partial_and_cancel():
    q = {}
    u, b, k, flag, path = _live("slices", q)
    s = u.products["SILVERM"]
    leg = next(iter(s.chains[s.expiries()[0]].values()))["CE"]
    q[leg.token] = _q(1500.0)
    b.place(leg.token, "B", 130 * leg.lot, "MKT")              # 130 lots, freeze 120 lots
    sent = [int(x["quantity"]) for x in k.sent]
    check("freeze split 600 + 50", sent == [600, 50], sent)
    check("distinct tags", len({x["tag"] for x in k.sent}) == 2)
    o1, o2 = b.orders[-2], b.orders[-1]
    k.fill(o1["broker_id"], 300, 1500.0)
    b.poll(); b.sync_positions()
    check("partial stays open, position 300", o1["status"] == "OPEN" and b.pos[leg.token]["net"] == 300, (o1, b.pos))
    r = b.cancel(o2["id"])
    b.poll()
    check("cancel goes to Kotak", r["ok"] and k.cancels == [o2["broker_id"]] and o2["status"] == "CANCELLED", (r, o2))
    check("cap still applies", not b.place(leg.token, "B", 301 * leg.lot, "MKT")["ok"])
    path.unlink(missing_ok=True)


def test_mirror_manual_trades_and_hand_closed_positions():
    """The 26/27-Oct problem: positions closed by hand in Kotak must vanish from the terminal,
    and positions opened by hand must appear — EXIT ALL then acts on Kotak's real positions only."""
    q = {}
    u, b, k, flag, path = _live("mirror", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    pe = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["PE"]
    q[ce.token], q[pe.token] = _q(500.0), _q(300.0)
    k.carry[(ce.symbol, "NRML")] = [0, 200, 0.0, 200 * 480.0]          # carried short 2 lots from yesterday
    b.sync_positions()
    check("overnight position read from Kotak", b.pos[ce.token]["net"] == -200 and abs(b.pos[ce.token]["avg"] - 480) < 1e-9, b.pos)
    k.hand_trade(ce.symbol, "B", 200, 470.0)                            # owner closes it by hand in Kotak
    k.hand_trade(pe.symbol, "S", 100, 301.0)                            # and sells a put by hand
    b.sync_positions()
    check("hand-closed position gone", b.pos[ce.token]["net"] == 0, b.pos)
    check("realized from Kotak's prices", abs(b.pos[ce.token]["realized"] - (480 - 470) * 200) < 1e-6, b.pos[ce.token])
    check("hand-opened position shown", b.pos[pe.token]["net"] == -100, b.pos)
    n = len(k.sent)
    r = b.exit_all()
    sent = k.sent[n:]
    check("exit all closes ONLY what Kotak holds (buy 1 put lot)", len(sent) == 1 and sent[0]["trading_symbol"] == pe.symbol
          and sent[0]["transaction_type"] == "B" and sent[0]["quantity"] == "100", sent)
    check("never buys back the hand-closed call", not any(x["trading_symbol"] == ce.symbol for x in sent), sent)
    path.unlink(missing_ok=True)


def test_exit_all_flattens_broker_parallel_and_verifies():
    q = {}
    u, b, k, flag, path = _live("exitall", q)
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    a, c2, d = ch[9300.0]["CE"], ch[9300.0]["PE"], ch[9400.0]["CE"]
    for t in (a, c2, d):
        q[t.token] = _q(200.0)
    k.carry[(a.symbol, "NRML")] = [0, 200, 0.0, 40000.0]                # short
    k.carry[(c2.symbol, "NRML")] = [0, 100, 0.0, 20000.0]               # short
    k.carry[(d.symbol, "MIS")] = [100, 0, 20000.0, 0.0]                 # long hedge, MIS
    working = k.hand_trade(a.symbol, "S", 100, 250.0, status="open")    # a working manual order
    k.instant = "complete"
    k.slow[c2.symbol] = 1.5                                             # one Kotak call hangs
    t0 = time.time()
    r = b.exit_all()
    check("working manual order cancelled first", working in k.cancels, k.cancels)
    sides = [(x["trading_symbol"], x["transaction_type"], x["product"], x["quantity"]) for x in k.sent]
    check("all 3 legs sent", len(sides) == 3, sides)
    ta = next(x["_t"] for x in k.sent if x["trading_symbol"] == a.symbol)
    check("a slow Kotak call does not hold the others back", ta - t0 < 1.0, ta - t0)
    check("shorts closed before the long", [s[1] for s in sides][-1] == "S", sides)
    check("long closed in ITS product (MIS)", (d.symbol, "S", "MIS", "100") in sides, sides)
    check("verified flat with Kotak", r["ok"] and r["state"] == "filled" and "FLAT" in r["msg"], r)
    b.sync_positions()
    check("terminal flat = Kotak flat", all(not p["net"] for p in b.pos.values()), b.pos)
    log = audit.read(dt.datetime.now(audit.IST).date().isoformat())
    check("exit all in the audit log", any(e["kind"] == "exit-all" and "FLAT" in e.get("what", "") for e in log), log[:3])
    path.unlink(missing_ok=True)


def test_exit_all_pushes_slow_exits_and_reports_not_flat():
    q = {}
    u, b, k, flag, path = _live("exitpush", q)
    a = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[a.token] = _q(200.0)
    k.carry[(a.symbol, "NRML")] = [0, 100, 0.0, 20000.0]
    r = b.exit_all()                                                    # exit rests (no instant fill)
    check("slow exit pushed through the book", len(k.mods) >= 1 and float(k.mods[0]["price"]) > 200.5, k.mods)
    check("not flat -> red, names the position", not r["ok"] and "NOT FLAT" in r["msg"] and a.symbol in r["msg"], r)
    k2 = FakeKotak()
    u, b2, k2, _, p2 = _live("exitpush2", q, fake=None)
    b2._client_override = k2
    k2.carry[(a.symbol, "NRML")] = [0, 100, 0.0, 20000.0]
    k2.fill_on_modify = True
    r = b2.exit_all()
    check("pushed exit fills -> FLAT", r["ok"] and "FLAT" in r["msg"], r)
    path.unlink(missing_ok=True)
    p2.unlink(missing_ok=True)


def test_exit_all_refuses_when_kotak_unreadable():
    q = {}
    u, b, k, flag, path = _live("unread", q)
    a = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[a.token] = _q(200.0)
    k.carry[(a.symbol, "NRML")] = [0, 100, 0.0, 20000.0]
    b.sync_positions()
    k.pos_fail = True
    r = b.exit_all()
    check("nothing sent on stale data", not r["ok"] and not k.sent and "cannot read Kotak" in r["msg"], r)
    check("positions marked not readable", not b.positions_fresh() and b.pos[a.token]["net"] == -100, b.pos_sync)
    r = b.exit_position(a.token)
    check("single exit also refuses", not r["ok"] and not k.sent, r)
    path.unlink(missing_ok=True)


def test_not_armed_blocks_new_risk_allows_exit():
    q = {}
    u, b, k, flag, path = _live("arm", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = _q(500.0)
    k.instant = "complete"
    b.place(ce.token, "S", 200, "MKT")
    b.sync_positions()
    flag["armed"] = False
    n = len(k.sent)
    r = b.place(ce.token, "S", 100, "MKT")
    check("disarmed: new risk refused", not r["ok"] and "not armed" in r["msg"] and len(k.sent) == n, r)
    r = b.place(ce.token, "B", 300, "MKT")
    check("disarmed: bigger-than-position buy refused", not r["ok"], r)
    b.settings = lambda: {"entry_ref": "touch", "entry_buffer": 1, "entry_unit": "%", "exit_ref": "touch",
                          "exit_buffer": 1, "exit_unit": "%", "reprice": False, "market_after_s": 0,
                          "product": "MIS", "session": "AUTO"}
    r = b.exit_all()
    check("disarmed: exit all still works", r["ok"] and not b.pos[ce.token]["net"], (r, b.pos))
    check("exit in the position's product (NRML), not the MIS setting", k.sent[-1]["product"] == "NRML", k.sent[-1])
    path.unlink(missing_ok=True)


def test_reply_lost_matched_by_tag_and_restart():
    q = {}
    u, b, k, flag, path = _live("recover", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = _q(500.0)
    o = {"id": 50, "ts": "", "t0": time.time(), "token": ce.token, "symbol": ce.symbol, "name": ce.name, "side": "B",
         "qty": 100, "lot": 100, "kind": "MKT", "limit": 505.0, "filled": 0, "avg": 0.0, "status": "SENDING",
         "reason": "", "source": "", "role": "entry", "product": "NRML", "session": "AUTO", "t_rep": 0, "slice": "",
         "live": True, "tag": L.TAG + "202020002", "broker_id": None}
    b.orders.append(o)
    k.rows["888"] = {"nOrdNo": "888", "trdSym": ce.symbol, "trnsTp": "B", "qty": 100, "fldQty": 100, "avgPrc": "502.00",
                     "prc": "505.00", "ordSt": "complete", "prod": "NRML", "tag": o["tag"], "exSeg": "mcx_fo"}
    b.poll()
    check("reply-lost order matched by tag", o["broker_id"] == "888" and o["status"] == "COMPLETE", o)
    b._save()
    _, b2, _, _, _ = _live("recover", q, fake=k, path=path)
    check("restart: positions NOT taken from the file", b2.pos == {}, b2.pos)
    b2.sync_positions()
    check("restart: positions read from Kotak", b2.pos[ce.token]["net"] == 100, b2.pos)
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


def test_exit_instrument_only_that_instrument():
    q = {}
    u, b, k, flag, path = _live("exitinst", q)
    crude = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    gm = next(iter(u.products["GOLDM"].chains[u.products["GOLDM"].expiries()[0]].values()))["PE"]
    for t in (crude, gm):
        q[t.token] = _q(200.0)
    k.carry[(crude.symbol, "NRML")] = [0, 200, 0.0, 40000.0]
    k.carry[(gm.symbol, "NRML")] = [0, 100, 0.0, 20000.0]
    k.hand_trade(gm.symbol, "S", 100, 250.0, status="open")             # working GOLDM order: must survive
    k.instant = "complete"
    r = b.exit_instrument("CRUDEOIL", 1.0)
    check("only crude sent", [x["trading_symbol"] for x in k.sent] == [crude.symbol], k.sent)
    check("other instrument's working order untouched", not k.cancels, k.cancels)
    check("crude flat verified", r["ok"] and "FLAT" in r["msg"] and "CRUDEOIL" in r["msg"], r)
    b.sync_positions()
    check("goldm position still open", b.pos[gm.token]["net"] == -100, b.pos)
    r = b.exit_instrument("GOLD", 1.0)                                  # big GOLD: nothing there
    check("GOLD never catches GOLDM", r["ok"] and "no open GOLD" in r["msg"] and len(k.sent) == 1, r)
    n = len(k.sent)
    r = b.exit_instrument("GOLDM", 0.5)
    check("50% of a 1-lot position = 1 lot", k.sent[n]["quantity"] == "100" if len(k.sent) > n else False, k.sent[n:])
    path.unlink(missing_ok=True)


def test_exit_timers():
    from live_trading_options.mcx_terminal import sched as SC
    clock = {"t": dt.datetime(2026, 10, 28, 22, 0, 0, tzinfo=SC.IST)}
    fired = []
    p = Path(tempfile.gettempdir()) / "mcx_sched_test.json"
    p.unlink(missing_ok=True)
    tm = SC.ExitTimers(lambda mode, name, frac: fired.append((mode, name, frac)) or {"ok": True, "msg": "FLAT"},
                       path=p, now=lambda: clock["t"])
    check("bad time refused", not tm.set("CRUDEOIL", 1.0, "25:00", "LIVE")["ok"])
    check("past time refused", not tm.set("CRUDEOIL", 1.0, "21:59:59", "LIVE")["ok"])
    check("bad % refused", not tm.set("CRUDEOIL", 0.3, "23:00", "LIVE")["ok"])
    check("HH:MM accepted as HH:MM:00", tm.set("CRUDEOIL", 1.0, "23:20", "LIVE")["ok"] and tm.view()[0]["at"] == "23:20:00")
    tm.set("CRUDEOIL", 1.0, "23:25:30", "LIVE")
    check("setting again replaces", len(tm.view()) == 1 and tm.view()[0]["at"] == "23:25:30", tm.view())
    tm.set("GOLDM", 0.5, "23:10:00", "PAPER")
    clock["t"] = clock["t"].replace(hour=23, minute=10, second=0)
    tm.tick()
    check("fires at its time, on the book it was set in", fired == [("PAPER", "GOLDM", 0.5)], fired)
    tm.tick()
    check("fires once", len(fired) == 1, fired)
    tm2 = SC.ExitTimers(lambda *a: {"ok": True}, path=p, now=lambda: clock["t"])
    check("survives a restart", any(x["name"] == "CRUDEOIL" and x["state"] == "armed" for x in tm2.view()), tm2.view())
    check("clear", tm.clear("CRUDEOIL", 1.0)["ok"] and not any(x["state"] == "armed" for x in tm.view()), tm.view())
    clock["t"] = clock["t"].replace(minute=30)
    tm.tick()
    check("cleared timer never fires", len(fired) == 1, fired)
    p.unlink(missing_ok=True)


def test_late_fill_notice_and_timeout_kept_sending():
    q = {}
    u, b, k, flag, path = _live("speed", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = _q(500.0)
    notes = []
    b.notify = notes.append
    r = b.place(ce.token, "S", 100, "MKT")
    check("pop-up says working", r["ok"] and r["state"] == "waiting", r)
    o = b.orders[-1]
    k.fill(o["broker_id"], 100, 499.0)
    b.poll()
    check("late fill pushed as a notice", len(notes) == 1 and notes[0]["ok"] and "SOLD 1 lot" in notes[0]["msg"], notes)
    # Kotak does not answer in time: the order is NOT called rejected; found later by its tag
    real = k.place_order

    def slow_reply(**kw):
        real(**kw)
        return {"Error": "HTTPSConnectionPool: Read timed out. (read timeout=8.0)"}
    k.place_order = slow_reply
    r = b.place(ce.token, "S", 100, "MKT")
    o2 = b.orders[-1]
    check("timeout -> still SENDING, not REJECTED", o2["status"] == "SENDING" and "checking" in o2["reason"], o2)
    b.poll()
    check("found at Kotak by its tag", o2["broker_id"] and o2["status"] in ("OPEN", "COMPLETE"), o2)
    path.unlink(missing_ok=True)


def test_fastrest_shim():
    from live_trading_options.mcx_terminal import fastrest as FR
    seen = {}

    class S:
        def post(self, url, **kw):
            seen.update(kw)
            return "ok"
    sh = FR._Shim()
    sh._local.s = S()
    check("post goes through the kept-open session", sh.post("https://x", data={}) == "ok")
    check("every call gets a timeout", seen.get("timeout") == (FR.CONNECT_TIMEOUT, FR.READ_TIMEOUT), seen)
    check("other requests names still there", sh.exceptions is __import__("requests").exceptions)
    check("timeout text detected", FR.is_timeout("Read timed out. (read timeout=8.0)") and not FR.is_timeout("RMS reject"))


def test_audit_append_only():
    day = dt.datetime.now(audit.IST).date().isoformat()
    p = audit._path(day)
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    audit.write("ui", "me@x.com", what="exitall: EXIT ALL")
    audit.write("op", "me@x.com", op="exit_all", result="FLAT")
    after = p.read_text(encoding="utf-8")
    check("only appended", after.startswith(before) and after.count("\n") == before.count("\n") + 2)
    rows = audit.read(day)
    check("newest first", rows[0]["kind"] == "op" and rows[1]["kind"] == "ui", rows[:2])
    check("who + time recorded", rows[0]["user"] == "me@x.com" and len(rows[0]["t"]) == 12, rows[0])


def test_restart_schedule():
    from live_trading_options.mcx_terminal import server as SV
    IST = SV.IST
    at = lambda h, m, d=28: dt.datetime(2026, 10, d, h, m, tzinfo=IST)
    check("born 09:00 -> reset 00:05 next day", SV.next_restart(at(9, 0)) == at(0, 5, 29))
    check("born 00:05:30 -> refresh 08:45", SV.next_restart(at(0, 5) + dt.timedelta(seconds=30)) == at(8, 45))
    check("born 23:59 -> reset 00:05", SV.next_restart(at(23, 59)) == at(0, 5, 29))


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
    check("non-owner cannot arm", not r["ok"] and e.mode == "PAPER", r)
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
    sys.stdout.flush()
    import os
    os._exit(1 if FAIL else 0)
