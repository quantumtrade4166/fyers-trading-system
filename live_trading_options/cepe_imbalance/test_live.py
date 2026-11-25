"""
cepe_imbalance/test_live.py — the LIVE (Kotak D) book against a FAKE Kotak, offline. No network, no orders.

    .venv\\Scripts\\python.exe -m pytest live_trading_options/cepe_imbalance/test_live.py -q
"""
import itertools
import math
import time
from pathlib import Path

import pytest

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import instruments as I
from live_trading_options.cepe_imbalance import live as LV
from live_trading_options.cepe_imbalance.engine import Engine
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.cepe_imbalance.strategy import CePeImbalance

TD = Path(__file__).resolve().parent / "testdata"
N, B = "NIFTY", "NIFTY_LIVE"


@pytest.fixture(scope="module")
def universe():
    texts = {s: (TD / f"scrip_{s}_sample.csv").read_text(encoding="utf-8") for s in ("nse_fo", "bse_fo", "mcx_fo", "nse_com")}
    return I.build(texts, today="2026-11-02")


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    for k, v in (("WAIT_FIRST_S", 0.05), ("WAIT_RETRY_S", 0.05), ("CONFIRM_S", 0.05), ("POLL_S", 0.0),
                 ("POS_AFTER_FILL_S", 0.0), ("EXIT_RETRY_S", 0.0)):
        monkeypatch.setattr(LV, k, v)


def model(k, kind, f):
    intr = max(f - k, 0) if kind == "CE" else max(k - f, 0)
    return round(intr + 150 * math.exp(-((k - f) / 500) ** 2), 1)


class FakeKotak:
    """Kotak REST as the live book uses it. Fills a marketable limit at the touch at once (unless told
    otherwise); positions from its own fills (+ `other` = other strategies on the account)."""

    def __init__(self, universe):
        self.by_sym = {c.symbol: c for c in universe.by_key.values()}
        self.px = {}                                  # symbol -> (bid, ask)
        self.orders = {}                              # oid -> row
        self.ids = itertools.count(1000)
        self.reject = {}                              # symbol -> reason (every order on it)
        self.no_fill = set()                          # symbols whose orders rest unfilled
        self.ignore_cancel = set()
        self.other = {}                               # key -> extra net units held by other strategies
        self.drop = {}                                # key -> units removed (closed by hand in the app)
        self.used = 0.0
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(("place", kw))
        oid = str(next(self.ids))
        sym, side, qty, lim = kw["trading_symbol"], kw["transaction_type"], int(kw["quantity"]), float(kw["price"])
        row = {"nOrdNo": oid, "trdSym": sym, "trnsTp": side, "qty": str(qty), "prc": f"{lim:.2f}",
               "ordSt": "open", "fldQty": "0", "avgPrc": "0", "exSeg": kw["exchange_segment"]}
        self.orders[oid] = row
        if sym in self.reject:
            row.update(ordSt="rejected", rejRsn=self.reject[sym])
        elif sym not in self.no_fill:
            bid, ask = self.px[sym]
            if (side == "S" and lim <= bid) or (side == "B" and lim >= ask):
                row.update(ordSt="complete", fldQty=str(qty), avgPrc=str(bid if side == "S" else ask))
                self.used += 50000 if side == "S" else -50000
        return {"nOrdNo": oid, "stat": "Ok", "stCode": 200}

    def order_report(self):
        return {"data": [dict(r) for r in self.orders.values()]}

    def cancel_order(self, order_id):
        r = self.orders[str(order_id)]
        if r["trdSym"] not in self.ignore_cancel and r["ordSt"] == "open":
            r["ordSt"] = "cancelled"
        return {"stat": "Ok"}

    def positions(self):
        net = {}
        for r in self.orders.values():
            c = self.by_sym[r["trdSym"]]
            q = int(r["fldQty"])
            net[c.key] = net.get(c.key, 0) + (q if r["trnsTp"] == "B" else -q)
        for k, v in self.other.items():
            net[k] = net.get(k, 0) + v
        for k, v in self.drop.items():
            net[k] = net.get(k, 0) + v
        rows = []
        for k, n in net.items():
            seg, tok = k.split("|")
            rows.append({"exSeg": seg, "tok": tok, "flBuyQty": str(max(n, 0)), "flSellQty": str(max(-n, 0)),
                         "cfBuyQty": "0", "cfSellQty": "0"})
        return {"data": rows} if rows else {"stat": "Not_Ok", "errMsg": "No Data"}

    def limits(self):
        return {"MarginUsed": str(self.used), "Net": "5430000", "stat": "Ok"}

    def quotes(self, instrument_tokens, quote_type):
        return []                                     # no depth: the book falls back to its own quote


class Rig:
    def __init__(self, universe, f=25600.0, persist=False, armed=True, tmp=None):
        self.u = universe
        self.m = Market(universe)
        self.k = FakeKotak(universe)
        acct = LV.Account(client=self.k, start=False)
        ctl = LV.Control(path=(tmp or Path(".")) / "control_test.json") if tmp else _MemControl()
        self.ctx = LV.LiveCtx(acct, ctl, sleep=lambda s: None)
        self.eng = Engine(universe, self.m, persist=persist, live=self.ctx)
        self.r = self.eng.runners[B]
        self.r.ctx.in_session = lambda now=None: True
        self.e = self.m.expiry(N)
        self.ctx.control.set(B, armed)
        self.f = f
        self.move(f, run=False)
        self.eng.refresh_subscriptions()

    def move(self, f, run=True, override=None):
        self.f = f
        override = override or {}
        und = self.u.underlyings[N]
        ticks = [Tick(und.spot_key, f, None, None, None, None, None, None, 0, time.time())]
        for k in range(int(f) - 1500, int(f) + 1501, 50):
            for kind in ("CE", "PE"):
                c = self.m.contract(N, self.e, float(k), kind)
                if c:
                    px = override.get((k, kind), model(k, kind, f))
                    bid, ask = round(px - 0.5, 2), round(px + 0.5, 2)
                    self.k.px[c.symbol] = (bid, ask)
                    ticks.append(Tick(c.key, px, bid, ask, 1, 1, 0, 0, 0, time.time()))
        if run:
            self.eng.on_ticks(ticks)
        else:
            self.m.apply(ticks)

    def legs(self):
        return {k: dict(sorted(v.items())) for k, v in CePeImbalance.legs(self.r.ctx).items()}

    def open_lots(self):
        return sum(abs(p.lots) for p in self.r.book.positions.values())

    def broker_net(self):
        self.ctx.account.read_positions()
        return {k: v for k, v in self.ctx.account.pos.items() if v}


class _MemControl(LV.Control):
    def __init__(self):
        super().__init__(path=None)
        self.d = {}

    def read(self):
        return self.d

    def set(self, name, armed, by="test"):
        self.d[name] = {"armed": bool(armed)}
        return self.d[name]


def test_no_live_books_without_kotak(universe):
    eng = Engine(universe, Market(universe), persist=False)
    assert not any(C.is_live(n) for n in eng.runners)


def test_live_defaults(universe):
    g = Rig(universe)
    assert g.r.live and g.r.params["lots"] == 1 and g.r.params["mtm_stop"] == 5000
    assert g.r.auto == {"auto_start": True, "auto_time": "09:16"}
    assert g.r.strategy_name == "cepe_flex"                # owner 17-Nov: the FLEX rules


def test_start_refused_when_not_armed(universe):
    g = Rig(universe, armed=False)
    assert not g.eng.start(B)["ok"]
    assert not g.k.orders


def test_entry_real_orders_and_own_book(universe):
    g = Rig(universe)
    assert g.eng.start(B)["ok"]
    assert g.r.state["phase"] == "in"
    sells = [kw for t, kw in g.k.calls if t == "place"]
    assert len(sells) == 6 and all(kw["transaction_type"] == "S" and kw["product"] == "NRML" for kw in sells)
    assert all(kw["quantity"] == "65" and kw["order_type"] == "L" for kw in sells)
    tags = [kw["tag"] for kw in sells]
    assert len(set(tags)) == 6 and all(t.startswith("cpek") for t in tags)
    lg = g.legs()
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()) == 3
    # fills booked at Kotak's price (the bid), not at our limit
    t = g.r.book.trades[0]
    assert t["live"] and t["price"] == g.k.px[t["symbol"]][0]
    assert g.broker_net() == {k: -65 for k in g.r.book.positions}


def test_adjustment_rolls_with_real_orders_and_stays_balanced(universe):
    g = Rig(universe)
    g.r.params["mtm_stop"] = 0                             # rules only (the stop has its own test)
    g.eng.start(B)
    n0 = len(g.k.orders)
    for f in range(25610, 26001, 10):
        g.move(f)
    assert len(g.k.orders) > n0                           # adjustments went to Kotak
    lg = g.legs()
    assert sum(lg["CE"].values()) == sum(lg["PE"].values()) == 3
    assert g.r.book.fault is None and g.r.running
    own = {k: round(p.lots * 65) for k, p in g.r.book.positions.items() if p.lots}
    assert g.broker_net() == own                          # own book == Kotak, contract by contract


def test_margin_rejection_on_entry_flattens_and_halts(universe):
    g = Rig(universe)
    atm = g.m.atm(N, g.e)
    bad = g.m.contract(N, g.e, atm - 100, "PE")            # the last PE of the entry
    g.k.reject[bad.symbol] = "RMS:Margin Exceeds,Required:186000"
    g.eng.start(B)
    assert g.r.state["phase"] == "halted" and not g.r.running
    assert "MARGIN" in g.r.book.fault
    assert g.open_lots() == 0 and g.broker_net() == {}    # everything sold was bought back
    g.k.reject.clear()                                     # margin added: a fresh Start enters cleanly
    assert g.eng.start(B)["ok"] and g.r.state["phase"] == "in" and g.r.book.fault is None
    assert g.open_lots() == 6


def test_unfilled_sell_is_retried_then_halts(universe):
    g = Rig(universe)
    atm = g.m.atm(N, g.e)
    stuck = g.m.contract(N, g.e, atm, "CE")
    g.k.no_fill.add(stuck.symbol)
    g.eng.start(B)
    tries = [kw for t, kw in g.k.calls if t == "place" and kw["trading_symbol"] == stuck.symbol]
    assert len(tries) == LV.ATTEMPTS["SELL"]
    assert g.r.state["phase"] == "halted" and g.open_lots() == 0 and g.broker_net() == {}


def test_mtm_stop_exits_everything(universe):
    g = Rig(universe)
    g.r.params["mtm_stop"] = 2000
    g.eng.start(B)
    g.move(27000)                                          # huge move: CE side deep loss
    assert g.r.state["phase"] in ("stopped", "halted")
    assert g.open_lots() == 0 and g.broker_net() == {}


def test_buyback_never_bigger_than_kotak_holds(universe):
    g = Rig(universe)
    g.eng.start(B)
    k = next(iter(g.r.book.positions))
    g.k.drop[k] = 65                                       # owner closed this leg by hand in the Kotak app
    for key in list(g.r.book.last_fill_ts):                # the fills are > POS_AFTER_FILL_S old (was timing-flaky)
        g.r.book.last_fill_ts[key] -= LV.POS_AFTER_FILL_S + 1
    g.ctx.account.read_positions()
    out = g.eng.stop(B, exit_positions=True)
    assert g.r.book.fault and "closed outside" in g.r.book.fault
    assert g.broker_net() == {}                            # no long created on that contract
    assert g.open_lots() == 0


def test_other_strategy_short_on_same_contract_is_untouched(universe):
    g = Rig(universe)
    g.eng.start(B)
    k = next(iter(g.r.book.positions))
    g.k.other[k] = -130                                    # ST Directional also short 2 lots there
    g.eng.stop(B, exit_positions=True)
    assert g.broker_net() == {k: -130}                     # only OUR lots were bought back


def test_unsettled_order_blocks_new_orders(universe):
    g = Rig(universe)
    atm = g.m.atm(N, g.e)
    c = g.m.contract(N, g.e, atm, "CE")
    g.k.no_fill.add(c.symbol)
    g.k.ignore_cancel.add(c.symbol)                        # Kotak never confirms the cancel
    g.eng.start(B)
    assert g.r.book.unsettled
    n = len(g.k.orders)
    r = g.r.book.order(g.m.contract(N, g.e, atm + 50, "CE"), "SELL", 1, "x")
    assert not r["ok"] and len(g.k.orders) == n            # nothing new sent on top of an unknown order
    # Kotak finally reports it filled -> booked once, settled
    oid = next(iter(g.r.book.unsettled.values()))["oid"]
    g.k.orders[oid].update(ordSt="complete", fldQty="65", avgPrc="100")
    g.k.ignore_cancel.clear()
    g.r.book._settle()
    assert not g.r.book.unsettled
    assert g.r.book.positions[c.key].lots == -1


def _ten_am(r):
    import datetime as dt
    return dt.datetime.combine(dt.date.fromisoformat(r.date), dt.time(10, 0), tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))


@pytest.mark.parametrize("book", [B, "NIFTY_SYN_LIVE"])
def test_late_fill_on_halted_book_is_bought_back_by_the_worker(universe, book):
    """23-Nov-2026 live incident: Kotak FILLED the first entry SELL but its order book showed nothing for 7 s;
    the book halted thinking it held nothing, and the short sat alone at Kotak until Stop was pressed 5 h later.
    Now the live worker re-reads the unknown order every few seconds and buys a late fill back at once."""
    g = Rig(universe)
    if book != B:
        g.r = g.eng.runners[book]
        g.r.ctx.in_session = lambda now=None: True
        g.r.params["exit_time"] = ""
        g.ctx.control.set(book, True)
    atm = g.m.atm(N, g.e)
    c = g.m.contract(N, g.e, atm, "CE")
    g.k.no_fill.add(c.symbol)                              # Kotak's order book: still "open" ...
    g.k.ignore_cancel.add(c.symbol)                        # ... and the cancel goes nowhere
    g.eng.start(book)
    b = g.r.book
    assert b.unsettled and b.fault and g.r.state.get("phase") == "halted"
    assert not any(p.lots for p in b.positions.values())   # the book thinks it is flat
    # ... while Kotak had in fact filled it (shows up in its order book later)
    oid = next(iter(b.unsettled.values()))["oid"]
    g.k.orders[oid].update(ordSt="complete", fldQty="65", avgPrc="94.05")
    g.k.no_fill.clear()
    g.k.ignore_cancel.clear()
    n_orders = len(g.k.orders)
    b._last_settle = 0.0
    g.eng.live_timer(book, g.r, _ten_am(g.r))              # the worker's timer - no button pressed
    assert not b.unsettled
    evs = [e["event"] for e in g.r.events]
    assert "late_fill" in evs and "late_fill_exit" in evs, evs
    assert len(g.k.orders) == n_orders + 1                 # one BUY back
    assert not any(p.lots for p in b.positions.values())
    assert g.broker_net() == {}                            # Kotak flat for this contract
    assert "LATE FILL" in (g.r.state.get("blocked") or "")


def test_unsettled_recheck_is_throttled(universe):
    g = Rig(universe)
    atm = g.m.atm(N, g.e)
    c = g.m.contract(N, g.e, atm, "CE")
    g.k.no_fill.add(c.symbol)
    g.k.ignore_cancel.add(c.symbol)
    g.eng.start(B)
    b = g.r.book
    reads = []
    real = g.k.order_report
    g.k.order_report = lambda: reads.append(1) or real()
    b._last_settle = 0.0
    b.recheck_unsettled()
    first = len(reads)
    b.recheck_unsettled()                                  # again at once: skipped (every SETTLE_EVERY_S)
    assert first > 0 and len(reads) == first and b.unsettled


def test_disarmed_never_sells_but_buys_back(universe):
    g = Rig(universe)
    g.eng.start(B)
    g.ctx.control.set(B, False)
    atm = g.m.atm(N, g.e)
    r = g.r.book.order(g.m.contract(N, g.e, atm + 500, "CE"), "SELL", 1, "x")
    assert not r["ok"] and "not armed" in r["error"]
    out = g.r.book.exit_all("manual")
    assert all(x["ok"] for x in out) and g.open_lots() == 0


def test_never_goes_long(universe):
    g = Rig(universe)
    atm = g.m.atm(N, g.e)
    r = g.r.book.order(g.m.contract(N, g.e, atm, "CE"), "BUY", 1, "x")
    assert not r["ok"] and not g.k.orders


def test_reset_and_manual_orders_refused(universe):
    g = Rig(universe)
    assert not g.eng.reset(B)["ok"]
    assert not g.eng.manual_order(B, g.m.atm(N, g.e), "CE", "SELL", 1)["ok"]


def test_restart_reloads_own_book(universe, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    g = Rig(universe, persist=True, tmp=tmp_path)
    g.eng.start(B)
    before = {k: p.lots for k, p in g.r.book.positions.items() if p.lots}
    # a new engine on the same files (crash + restart)
    eng2 = Engine(universe, g.m, persist=True, live=g.ctx)
    r2 = eng2.runners[B]
    assert {k: p.lots for k, p in r2.book.positions.items() if p.lots} == before
    assert len(r2.book.orders) == 6 and r2.running and r2.state["phase"] == "in"


def test_open_positions_from_earlier_day_are_carried_and_flagged(universe, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    g = Rig(universe, persist=True, tmp=tmp_path)
    g.eng.start(B)
    held = {k: p.lots for k, p in g.r.book.positions.items() if p.lots}
    nxt = "2099-01-02"
    b2 = LV.KotakLiveBook(B, g.m, g.ctx, date=nxt, persist=True)
    assert {k: p.lots for k, p in b2.positions.items() if p.lots} == held
    assert b2.fault and "carried" in b2.fault


def test_session_end_square_off_even_when_stopped(universe):
    import datetime as dt
    g = Rig(universe)
    g.eng.start(B)
    g.eng.stop(B)                                          # stopped by hand, positions kept
    assert g.open_lots() == 6
    late = dt.datetime.combine(dt.date.today(), dt.time(15, 16), LV.IST)
    g.eng.live_timer(B, g.r, late)
    assert g.open_lots() == 0 and g.broker_net() == {}


def test_margin_view(universe):
    g = Rig(universe)
    g.eng.start(B)
    g.ctx.account.read_limits()
    v = g.r.book.live_view()
    assert v["armed"] and v["account"]["margin_used"] == 300000
    assert v["book_margin"] == 300000 and v["orders"] == 6


def test_slow_kotak_never_freezes_ticks_or_other_books(universe):
    """16-Nov fix: live orders run on the book's own worker. While Kotak takes its time, ticks keep
    flowing (prices/MTM move) and the engine lock is free for every other book."""
    import threading
    g = Rig(universe)
    g.eng.live_async = True                                # what run_timer switches on in the service
    slow = g.k.place_order

    def place(**kw):
        time.sleep(0.25)                                   # Kotak taking 250 ms per order
        return slow(**kw)
    g.k.place_order = place
    assert g.eng.start(B)["ok"] and g.r.pending_start and not g.k.orders     # entry is the worker's job
    w = threading.Thread(target=g.eng.live_step, args=(B,), kwargs={"timer": False})
    w.start()
    time.sleep(0.1)
    assert g.eng.live_busy.get(B)                          # the worker is placing orders now ...
    t0 = time.time()
    g.move(g.f + 10)                                       # ... and a tick batch still goes through at once
    assert time.time() - t0 < 0.2
    assert g.eng.lock.acquire(timeout=0.05)                # the engine lock is not held by the live book
    g.eng.lock.release()
    und = g.u.underlyings[N]
    assert g.m.quote(und.spot_key).ltp == g.f              # the new price is in the market already
    w.join(5)
    assert g.r.state["phase"] == "in" and g.open_lots() == 6
    # ticks for the live book are queued for its worker, then evaluated there
    g.move(g.f + 10)
    assert g.eng._live_keys[B]
    g.eng.live_step(B, timer=False)
    assert not g.eng._live_keys[B]


def test_normal_kotak_replies_never_force_a_login():
    """16-Nov: 260 needless Kotak logins (each dropped the feed) - "401" inside numbers of OK replies."""
    ok = [{"nOrdNo": "261116000024010", "stat": "Ok", "stCode": 200},
          {"stat": "Ok", "stCode": 200, "data": [{"actId": "17IC2", "tok": "45401", "flSellQty": "130"}]},
          {"MarginUsed": "1733545.40192", "Net": "4779616.41", "stat": "Ok", "stCode": 200},
          {"stat": "Not_Ok", "errMsg": "No Data", "stCode": 5203},
          [{"nOrdNo": "4011401"}]]
    assert not any(LV.session_error(r) for r in ok)
    bad = [{"stat": "Not_Ok", "errMsg": "Invalid session, please login again", "stCode": 401},
           {"Error": "401 Client Error: Unauthorized"}, {"code": "401", "message": "x"},
           {"Error": "Complete the 2fa process before accessing this application"}]
    assert all(LV.session_error(r) for r in bad)


def test_relogin_at_most_once_a_minute():
    class S:
        n = 0

        def get(self, force=False):
            if force:
                S.n += 1
            return self

        @property
        def client(self):
            return self

        def positions(self):
            return {"stat": "Not_Ok", "errMsg": "Invalid session", "stCode": 401}
    a = LV.Account(sessions=S(), start=False)
    for _ in range(5):
        a.call("positions")
    assert S.n == 1


# ── 25-Nov-2026: Kotak answered "time out" but FILLED the order ─────────────
def test_time_out_reply_is_never_retried_blind(universe, monkeypatch):
    """Kotak replied {"Error": "time out"} to two BUYs it had in fact filled; the book took them as refused and
    sent a third -> 3 fills instead of 1 (a 3-lot LONG). Now an unclear reply is looked for in Kotak's order book
    and nothing is re-sent while it may exist."""
    monkeypatch.setattr(LV, "LOST_WAIT_S", 0.2)
    g = Rig(universe)
    assert g.eng.start(B)["ok"]
    k = g.k
    real = k.place_order
    flaky = {"n": 0}

    def place_order(**kw):
        r = real(**kw)
        if kw["transaction_type"] == "B" and flaky["n"] < 2:     # first two BUYs: filled, but the reply is lost
            flaky["n"] += 1
            return {"Error": "time out"}
        return r
    k.place_order = place_order
    c = next(p for p in g.r.book.positions.values() if p.lots and p.kind == "CE")
    con = g.u.by_key[c.key]
    out = g.r.book.order(con, "BUY", 1, "test buy back")
    buys = [kw for t, kw in k.calls if t == "place" and kw["transaction_type"] == "B" and kw["trading_symbol"] == con.symbol]
    assert len(buys) == 1, f"re-sent a BUY after an unclear reply: {len(buys)} sent"
    assert out["ok"], out                                       # found in Kotak's book -> booked as filled
    assert g.r.book.positions[c.key].lots == 0
    assert g.broker_net().get(c.key, 0) == 0                    # Kotak flat on that leg, no long


def test_is_timeout_words():
    for t in ("time out", "Read timed out", "HTTPSConnectionPool: Max retries exceeded", "502 Bad Gateway",
              "Gateway Time-out", "Remote end closed connection", "timeout"):
        assert LV.is_timeout(t), t
    for t in ("RMS:Rule: Check circuit limit", "Insufficient margin", "Invalid price"):
        assert not LV.is_timeout(t), t


def test_reconnect_replay_of_old_prices_is_ignored(universe):
    """10:07 on 25-Nov: after a reconnect Kotak replayed 09:44 prices; they must not overwrite fresher ones."""
    from live_trading_options.cepe_imbalance.feed import Tick
    m = Market(universe)
    now = time.time()
    m.apply([Tick("bse_fo|1", 300.0, 299.0, 301.0, 1, 1, None, None, int(now), now)])
    m.apply([Tick("bse_fo|1", 420.0, 419.0, 421.0, 1, 1, None, None, int(now - 23 * 60), now)])   # 23 min old
    assert m.quote("bse_fo|1").ltp == 300.0 and m.stale_dropped == 1
    m.apply([Tick("bse_fo|1", 305.0, 304.0, 306.0, 1, 1, None, None, int(now - 30), now)])          # quiet option: old trade time
    assert m.quote("bse_fo|1").ltp == 305.0                    # < 60 s old: still taken
