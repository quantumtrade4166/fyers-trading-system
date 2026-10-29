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
        self.gen_den = {}        # symbol -> genDen (Kotak divides GOLDM amounts by 10)

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
        if kw.get("order_type") == "SL":
            row.update(ordSt="trigger pending", trgPrc=kw.get("trigger_price"))
        elif self.instant == "complete":
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
        g = lambda s: self.gen_den.get(s, 1)
        return {"data": [{"trdSym": s, "prod": p, "exSeg": "mcx_fo", "flBuyQty": str(a[0]), "flSellQty": str(a[1]),
                          "buyAmt": f"{a[2] / g(s):.2f}", "sellAmt": f"{a[3] / g(s):.2f}", "cfBuyQty": str(a[4]),
                          "cfSellQty": str(a[5]), "cfBuyAmt": f"{a[6] / g(s):.2f}", "cfSellAmt": f"{a[7] / g(s):.2f}",
                          "genNum": "1", "genDen": str(g(s))}
                         for (s, p), a in agg.items()]}

    def trade_report(self):
        out = []
        for i, r in enumerate(list(self.rows.values()) + list(self.manual.values())):
            if int(r["fldQty"]):
                out.append({"trdSym": r["trdSym"], "trnsTp": r["trnsTp"], "fldQty": int(r["fldQty"]),
                            "avgPrc": r["avgPrc"], "exTm": f"28-Oct-2026 10:{i // 60:02d}:{i % 60:02d}"})
        return {"data": out} if out else {"stat": "Not_Ok", "errMsg": "No Data"}

    def cancel_order(self, order_id, **kw):
        self.cancels.append(order_id)
        (self.rows.get(order_id) or self.manual.get(order_id))["ordSt"] = "cancelled"
        return {"result": order_id, "stat": "Ok", "stCode": 200}

    def modify_order(self, **kw):
        self.mods.append(kw)
        r = self.rows[kw["order_id"]]
        r["prc"] = kw["price"]
        r["qty"] = int(kw.get("quantity") or r["qty"])
        if kw.get("order_type") == "SL":
            r["trgPrc"] = kw.get("trigger_price")
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


def test_order_push():
    from live_trading_options.mcx_terminal import orderfeed as OF
    q = {}
    u, b, k, flag, path = _live("push", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = _q(500.0)
    b.place(ce.token, "S", 100, "MKT")
    o = b.orders[-1]
    polls = []
    real_report = k.order_report
    k.order_report = lambda: polls.append(1) or real_report()
    # Kotak pushes the fill: applied at once, without reading the order book
    b.on_push({"nOrdNo": o["broker_id"], "ordSt": "complete", "fldQty": 100, "avgPrc": "498.50", "prc": "490.00"})
    check("push fills the order with no poll", o["status"] == "COMPLETE" and o["avg"] == 498.5 and not polls, o)
    b._pos_kick.clear()
    b.on_push_position({"trdSym": ce.symbol})
    check("position push -> positions re-read now", b._pos_kick.is_set())
    n = o["filled"]
    b.on_push({"nOrdNo": o["broker_id"], "ordSt": "complete", "fldQty": 100, "avgPrc": "498.50"})
    check("repeat push changes nothing", o["filled"] == n == 100)
    k.order_report = real_report
    # the push beats Kotak's reply to place_order: kept, applied when the reply lands
    real = k.place_order

    def push_first(**kw):
        r = real(**kw)
        b.on_push({"nOrdNo": r["nOrdNo"], "ordSt": "complete", "fldQty": 100, "avgPrc": "497.00"})
        return r
    k.place_order = push_first
    flag["armed"] = True
    r = b.place(ce.token, "S", 100, "LMT", limit=480.0)
    o2 = b.orders[-1]
    check("early push applied", o2["status"] == "COMPLETE" and o2["avg"] == 497.0, o2)
    # message parsing
    check("parse order", OF.parse('{"type":"order","data":{"nOrdNo":"1"}}') == ("order", {"nOrdNo": "1"}))
    check("parse connect ack", OF.parse('{"ak":"ok","type":"cn","task":"cn","msg":"connected"}')[0] == "cn")
    check("parse junk", OF.parse("not json")[0] is None and OF.parse('{"ak":"nk"}')[0] is None)
    path.unlink(missing_ok=True)


def test_auto_stop_loss():
    from live_trading_options.mcx_terminal import sl as SL
    q = {}
    u, b, k, flag, path = _live("autosl", q)
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    ce, pe = ch[9300.0]["CE"], ch[9300.0]["PE"]
    q[ce.token] = _q(100.0)
    q[pe.token] = _q(50.0)
    cfg = {"auto_sl": {"arm_on": "sell", "sl_auto": True, "sl_val": 50, "sl_unit": "%", "qty": "full",
                       "trail_auto": True, "trail_on": False, "trail_every": 10}}
    notes = []
    spath = path.with_name("autosl_test.json")
    spath.unlink(missing_ok=True)
    m = SL.AutoSL(b, lambda t: (q.get(t) or {}).get("ltp"), lambda: cfg, live=True, path=spath, notify=notes.append)
    b.before_reduce, b.before_flatten, b.on_push_other = m.before_reduce, m.before_flatten, m.on_push

    def step(ltp=None):
        if ltp is not None:
            q[ce.token] = _q(ltp)
        b.sync_positions()
        for r in m.recs.values():
            r["t_mod"] = 0.0
        m._rows_at = 0.0
        m.step()

    # a short opened by hand in Kotak -> a real SL BUY order at Kotak
    k.hand_trade(ce.symbol, "S", 100, 100.0)
    step()
    r = m.recs.get(ce.token)
    o = k.sent[-1]
    check("SL order sent for the short", r and r["state"] == "pending" and o["order_type"] == "SL"
          and o["transaction_type"] == "B" and float(o["trigger_price"]) == 150.0, (r, o))
    check("SL limit beyond trigger", float(o["price"]) == 157.5, o["price"])
    # long positions are left alone when AUTO ARM ON = sell
    k.hand_trade(pe.symbol, "B", 100, 50.0)
    step()
    check("buy position not armed (sell only)", pe.token not in m.recs)
    # trail: 10 % of entry = 10 pts; price falls 11 -> stop 140, falls 21 -> 130
    step(89.0)
    check("trailed one step", m.recs[ce.token]["trigger"] == 140.0 and float(k.mods[-1]["trigger_price"]) == 140.0,
          (m.recs[ce.token], k.mods[-1:]))
    step(79.0)
    check("trailed two steps", m.recs[ce.token]["trigger"] == 130.0)
    step(95.0)
    check("never trails back", m.recs[ce.token]["trigger"] == 130.0)
    # added a lot -> the SL order grows with it
    k.hand_trade(ce.symbol, "S", 100, 100.0)
    step()
    check("SL qty follows the position", m.recs[ce.token]["qty"] == 200 and int(k.mods[-1]["quantity"]) == 200, k.mods[-1:])
    # terminal exit: SL order cancelled BEFORE the exit order goes out
    sl_oid = m.recs[ce.token]["oid"]
    n_sent = len(k.sent)
    res = b.exit_position(ce.token, 0.5)
    check("partial exit sent", res["ok"] and len(k.sent) == n_sent + 1, res)
    check("SL cancelled first", sl_oid in k.cancels and ce.token not in m.recs, k.cancels)
    k.fill(b.orders[-1]["broker_id"], 100, 96.0)
    for rr in m.pause:
        m.pause[rr] = 0
    step()
    check("re-armed for the rest, trail kept", m.recs[ce.token]["qty"] == 100 and m.recs[ce.token]["trigger"] == 130.0,
          m.recs.get(ce.token))
    # Kotak fills the SL -> HIT, flat, no re-arm
    sl_oid = m.recs[ce.token]["oid"]
    k.fill(sl_oid, 100, 131.0)
    step()
    check("hit announced", notes and "STOP-LOSS HIT" in notes[-1]["msg"], notes[-1:])
    check("flat after the hit -> no SL left", ce.token not in m.recs and not b.pos.get(ce.token, {}).get("net"))
    # SL filled at Kotak just before a terminal exit -> the exit is NOT sent
    k.hand_trade(ce.symbol, "S", 100, 100.0)
    step(100.0)
    sl_oid = m.recs[ce.token]["oid"]
    k.rows[sl_oid].update(fldQty=100, avgPrc="150.00", ordSt="complete")
    real_cancel = k.cancel_order
    k.cancel_order = lambda order_id, **kw: {"stat": "Not_Ok", "errMsg": "order already traded"}
    msg = m.before_reduce(ce.token)          # Kotak's positions may lag the fill: the hook still stops the exit
    check("exit blocked: SL already filled", msg and "already FILLED" in msg, msg)
    k.cancel_order = real_cancel
    step()
    # stop already crossed when found -> not placed
    k.hand_trade(ce.symbol, "S", 100, 100.0)
    m.pause.clear()
    step(160.0)
    check("stop already crossed -> not placed", m.recs[ce.token]["state"] == "past" and not m.recs[ce.token]["oid"],
          m.recs.get(ce.token))
    step(120.0)
    check("placed once back inside", m.recs[ce.token]["state"] == "pending")
    # a stray SL order at Kotak (no record) is cancelled
    k.n += 1
    k.rows["STRAY1"] = {"nOrdNo": "STRAY1", "trdSym": ce.symbol, "trnsTp": "B", "qty": 100, "fldQty": 0, "avgPrc": "0",
                        "prc": "200", "ordSt": "trigger pending", "prod": "NRML", "tag": "mcxs999", "exSeg": "mcx_fo"}
    step()
    check("stray SL order cancelled", "STRAY1" in k.cancels)
    # Kotak refuses SL orders -> the terminal watches and exits itself
    b.exit_position(ce.token, 1.0)
    k.fill(b.orders[-1]["broker_id"], 100, 120.0)
    step()
    k.refuse = "SL order not allowed"
    k.hand_trade(ce.symbol, "S", 100, 100.0)
    for rr in m.pause:
        m.pause[rr] = 0
    step(100.0)
    check("refused -> watching", m.recs[ce.token]["state"] == "watching" and "refused" in notes[-1]["msg"].lower(), m.recs.get(ce.token))
    k.refuse = None
    n_sent = len(k.sent)
    step(151.0)
    check("watched stop exits", len(k.sent) == n_sent + 1 and k.sent[-1]["transaction_type"] == "B"
          and k.sent[-1]["order_type"] == "L", k.sent[-1:])
    # settings validation
    check("settings: bad values refused", SL.clean({"sl_val": 50, "sl_unit": "pts"})["sl_unit"] == "pts")
    try:
        SL.clean({"sl_val": -1})
        check("negative SL refused", False)
    except ValueError:
        check("negative SL refused", True)
    spath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_goldm_amounts_and_open_entry():
    from live_trading_options.mcx_terminal import sl as SL
    q = {}
    u, b, k, flag, path = _live("goldm", q)
    exp = sorted(u.products["GOLDM"].chains)[0]
    ce = next(iter(u.products["GOLDM"].chains[exp].values()))["CE"]
    k.gen_den[ce.symbol] = 10
    # owner's 28-Oct trades: sold 11.0, bought back 11.5, sold again 14.5 -> short 1 lot opened at 14.5
    k.hand_trade(ce.symbol, "S", 100, 11.0)
    k.hand_trade(ce.symbol, "B", 100, 11.5)
    k.hand_trade(ce.symbol, "S", 100, 14.5)
    b.sync_positions()
    p = b.pos[ce.token]
    check("GOLDM average undoes Kotak's /10", p["net"] == -100 and abs(p["avg"] - 12.75) < 1e-6, p)
    check("GOLDM realized in rupees", abs(p["realized"] - (12.75 - 11.5) * 100 * ce.mult) < 1e-6, (p["realized"], ce.mult))
    check("open lot entry = 14.5 (oldest closed first)", b.open_entry(ce.token) == 14.5, b.open_entry(ce.token))
    q[ce.token] = _q(15.5)
    cfg = {"auto_sl": {"arm_on": "sell", "sl_auto": True, "sl_val": 50, "sl_unit": "%"}}
    spath = path.with_name("autosl_goldm.json")
    spath.unlink(missing_ok=True)
    m = SL.AutoSL(b, lambda t: (q.get(t) or {}).get("ltp"), lambda: cfg, live=True, path=spath)
    m.step()
    r = m.recs[ce.token]
    check("SL from 14.5 -> trigger 21.75, rounded to the 0.5 tick = 22 at Kotak", r["state"] == "pending" and r["entry"] == 14.5
          and r["trigger"] == 22.0, r)
    spath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_sl_apply_button():
    from live_trading_options.mcx_terminal import sl as SL
    q = {}
    u, b, k, flag, path = _live("slapply", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = _q(100.0)
    cfg = {"auto_sl": {"arm_on": "sell", "sl_auto": True, "sl_val": 50, "sl_unit": "%"}}
    spath = path.with_name("autosl_apply.json")
    spath.unlink(missing_ok=True)
    m = SL.AutoSL(b, lambda t: (q.get(t) or {}).get("ltp"), lambda: cfg, live=True, path=spath)
    k.hand_trade(ce.symbol, "S", 100, 100.0)
    b.sync_positions()
    m.step()
    first = m.recs[ce.token]["oid"]
    check("dropped with x -> no re-arm", m.drop(ce.token)["ok"] and (m.step() or ce.token not in m.recs))
    cfg["auto_sl"]["sl_val"] = 40
    r = m.apply()
    check("Apply puts it back at 40 %", r["ok"] and m.recs[ce.token]["trigger"] == 140.0
          and m.recs[ce.token]["oid"] != first and "140" in r["msg"], (r, m.recs.get(ce.token)))
    oid = m.recs[ce.token]["oid"]
    n_sent = len(k.sent)
    cfg["auto_sl"]["sl_val"] = 30
    r = m.apply()
    check("Apply moves the existing order (modify, no new order)", r["ok"] and len(k.sent) == n_sent
          and m.recs[ce.token]["oid"] == oid and float(k.mods[-1]["trigger_price"]) == 130.0, (r, k.mods[-1:]))
    q[ce.token] = _q(125.0)
    cfg["auto_sl"]["sl_val"] = 20
    r = m.apply()
    check("Apply refuses a level the price already passed", not r["ok"] and m.recs[ce.token]["trigger"] == 130.0, r)
    cfg["auto_sl"]["arm_on"] = "buy"
    check("Apply with nothing matching says so", "no open buy" in m.apply()["msg"])
    spath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_positions_row_controls():
    from live_trading_options.mcx_terminal import sl as SL
    q = {}
    u, b, k, flag, path = _live("rowctl", q)
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    ce = ch[9300.0]["CE"]
    for kk in (9300.0, 9350.0, 9400.0):
        q[ch[kk]["CE"].token] = _q(100.0)
    cfg = {"auto_sl": {"arm_on": "sell", "sl_auto": False, "sl_val": 50, "sl_unit": "pts", "trail_on": True,
                       "trail_every": 10, "trail_move": 5}}
    spath = path.with_name("autosl_row.json")
    spath.unlink(missing_ok=True)
    m = SL.AutoSL(b, lambda t: (q.get(t) or {}).get("ltp"), lambda: cfg, live=True, path=spath)
    k.hand_trade(ce.symbol, "S", 300, 100.0)
    b.sync_positions()
    p = b.pos[ce.token]
    check("sell value in rupees", p["sval"] == 30000.0 and p["bval"] == 0.0, p)
    m.step()
    check("Auto arm off -> no stop", ce.token not in m.recs)
    r = m.set_one(ce.token, True)
    check("row tick -> stop at the row's default (entry + 50 pts)", r["ok"] and m.recs[ce.token]["trigger"] == 150.0, r)
    r = m.set_one(ce.token, True, trigger=135)
    check("typed stop 135 moved at Kotak", r["ok"] and m.recs[ce.token]["trigger"] == 135.0
          and float(k.mods[-1]["trigger_price"]) == 135.0, r)
    r = m.set_one(ce.token, True, trigger=95)
    check("typed stop already crossed refused", not r["ok"] and m.recs[ce.token]["trigger"] == 135.0, r)
    r = m.set_one(ce.token, True, qty="half")
    check("Half -> 1 of 3 lots", r["ok"] and m.recs[ce.token]["qty"] == 100 and int(k.mods[-1]["quantity"]) == 100, r)
    # trail every 10 pts, move SL 5 pts: price 100 -> 79 = 2 steps -> stop 135 - 10 = 125
    q[ce.token] = _q(79.0)
    b.sync_positions()
    m.recs[ce.token]["t_mod"] = 0
    m.step()
    check("trail moves 5 per 10", m.recs[ce.token]["trigger"] == 125.0, m.recs[ce.token])
    r = m.set_one(ce.token, False)
    check("row untick -> stop cancelled, not re-armed", r["ok"] and ce.token not in m.recs and ce.token in m.done, r)
    # Away 1 of 3 lots: 9300 CE -> 9350 CE (no strike gap set -> next listed strike)
    b.before_reduce = m.before_reduce
    r = b.move(ce.token, 1, True, lots=1)
    check("partial Away sends 1 lot each leg", r["ok"] and all(int(o["quantity"]) == 100 for o in k.sent[-2:]), k.sent[-2:])
    check("Away legs: buy back 9300, sell 9350", {(o["trading_symbol"], o["transaction_type"]) for o in k.sent[-2:]}
          == {(ce.symbol, "B"), (ch[9350.0]["CE"].symbol, "S")}, k.sent[-2:])
    for o in b.orders[-2:]:
        k.fill(o["broker_id"], o["qty"], 100.0)
    b.sync_positions()
    m.pause.clear()
    r = b.move(ch[9350.0]["CE"].token, 1, False, lots=1)
    check("partial Move close: 9350 CE back to 9300 CE, 1 lot", r["ok"] and {(o["trading_symbol"], o["transaction_type"], int(o["quantity"]))
          for o in k.sent[-2:]} == {(ch[9350.0]["CE"].symbol, "B", 100), (ce.symbol, "S", 100)}, k.sent[-2:])
    check("move SL 0 = same as every", SL.clean({"trail_move": 0})["trail_move"] == 0.0)
    spath.unlink(missing_ok=True)
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
