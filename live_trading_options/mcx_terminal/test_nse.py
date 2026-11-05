"""
mcx_terminal/test_nse.py — offline tests for the NSE DESK (NIFTY, BANKNIFTY, SENSEX) with a FAKE Kotak client.
No network, no login, no real order can be sent from here.

The NSE desk is the MCX terminal with TERMINAL_DESK=NSE. What is different and tested here:
  * contracts from nse_fo + bse_fo, options priced off the index spot, NSE/BSE expiry encodings
  * every order / quote / subscription goes to the contract's own segment (nse_fo / bse_fo / nse_cm / bse_cm)
  * the feed's quantities are UNITS (MCX: lots)
  * OWN BOOK: live positions are only the desk's own fills; other strategies' trades on the same Kotak
    account are never shown, never cancelled, never exited; an exit is never bigger than Kotak holds

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_nse
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TERMINAL_DESK"] = "NSE"                     # before anything reads config.py
sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal import instruments as I
from live_trading_options.mcx_terminal import live as L
from live_trading_options.mcx_terminal import test_live as TL       # FakeKotak + fast timings
from live_trading_options.mcx_terminal import feed as F
from live_trading_options.mcx_terminal import paper as P

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.gettempdir()) / "nse_desk_test"
TMP.mkdir(exist_ok=True)
PASS = FAIL = 0
TODAY = "2026-11-04"


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name} {info}")


def universe():
    t = HERE / "testdata"
    return I.parse_many({"nse_fo": (t / "nse_fo_sample.csv").read_text(encoding="utf-8"),
                         "bse_fo": (t / "bse_fo_sample.csv").read_text(encoding="utf-8")}, today=TODAY)


class FakeNse(TL.FakeKotak):
    """Kotak look-alike whose positions carry the real segment of each symbol (nse_fo / bse_fo)."""

    def __init__(self, u):
        super().__init__()
        self.seg = {c.symbol: c.seg for c in u.by_token.values()}

    def positions(self):
        r = super().positions()
        for row in r.get("data") or []:
            row["exSeg"] = self.seg.get(row["trdSym"], "nse_fo")
        return r

    def hand_trade(self, sym, side, qty, px, prod="NRML", status="complete"):
        oid = super().hand_trade(sym, side, qty, px, prod, status)
        self.manual[oid]["exSeg"] = self.seg.get(sym, "nse_fo")
        return oid


def _q(ltp, spread=1.0):
    return {"ltp": ltp, "bid": ltp - spread / 2, "ask": ltp + spread / 2, "bq": 10000, "aq": 10000, "depth": None}


def _live(name, quotes, u=None, fake=None, path=None, armed=True):
    u = u or universe()
    path = path or TMP / f"own_{name}.json"
    if fake is None:
        path.unlink(missing_ok=True)
    fake = fake or FakeNse(u)
    b = L.LiveBook(u, lambda tok: quotes.get(tok, {}), sessions=None, armed=lambda: armed,
                   path=path, client=fake, start_poller=False)
    return u, b, fake, path


def legs(u):
    n = u.products["NIFTY"].chains["2026-11-09"]
    s = u.products["SENSEX"].chains["2026-11-05"]
    return n[24000.0]["CE"], n[24000.0]["PE"], s[78000.0]["CE"]


# ── contracts ───────────────────────────────────────────────────────────────
def test_desk_config():
    check("desk NSE", C.DESK == "NSE" and C.PORT == 47682 and C.DATA.name == "data_nse", (C.DESK, C.PORT, C.DATA))
    check("own book on", C.OWN_BOOK and L.OWN, C.OWN_BOOK)
    check("own tags", L.TAG == "nsed" and C.TAG_SL == "nses", (L.TAG, C.TAG_SL))
    check("not the MCX data folder", C.DATA != C.HERE / "data")


def test_universe():
    u = universe()
    n, b, s = (u.products[k] for k in ("NIFTY", "BANKNIFTY", "SENSEX"))
    check("lots 65 / 30 / 20", (n.lot, b.lot, s.lot) == (65, 30, 20), (n.lot, b.lot, s.lot))
    check("option tick 0.05", n.opt_tick == 0.05 and s.opt_tick == 0.05, (n.opt_tick, s.opt_tick))
    # NIFTY weekly is Tuesday; 10-Nov-2026 is a holiday -> Monday 9-Nov (symbol NIFTY26N09...)
    check("NIFTY weekly 9-Nov (1980-based expiry)", n.expiries()[0] == "2026-11-09", n.expiries())
    check("NIFTY symbol matches date", "26N09" in n.chains["2026-11-09"][24000.0]["CE"].symbol)
    check("SENSEX weekly Thursday 5-Nov (IST date)", s.expiries()[0] == "2026-11-05", s.expiries())
    check("BANKNIFTY monthly only", b.expiries()[0] == "2026-11-23", b.expiries())
    check("segments", n.chains["2026-11-09"][24000.0]["CE"].seg == "nse_fo"
          and s.chains["2026-11-05"][78000.0]["CE"].seg == "bse_fo")
    check("spot rows", (n.spot.seg, n.spot.token, s.spot.seg, s.spot.token, b.spot.token) == ("nse_cm", 26000, "bse_cm", 1, 26009),
          (n.spot, s.spot))
    check("options priced off the index", n.underlying("2026-11-09") is n.spot and s.underlying("2026-11-19") is s.spot)
    check("futures kept", [f.symbol for f in n.futures][:1] == ["NIFTY26NOVFUT"], n.futures[:1])
    check("freeze from the list", n.chains["2026-11-09"][24000.0]["CE"].freeze == 1801)
    check("strike gap 50 / 100", sorted(n.chains["2026-11-09"])[1] - sorted(n.chains["2026-11-09"])[0] == 50
          and sorted(s.chains["2026-11-05"])[1] - sorted(s.chains["2026-11-05"])[0] == 100)
    check("no MCX product", set(u.products) == {"NIFTY", "BANKNIFTY", "SENSEX"})


def test_feed_segments_and_units():
    u = universe()
    ce, pe, sx = legs(u)

    class WS:
        def __init__(self):
            self.sent = []

        def send(self, m):
            self.sent.append(json.loads(m))

    f = F.KotakFeed(None, lambda t: None, seg_of=lambda t: u.by_token[t].seg)
    f.ws = WS()
    f._send_subs([ce.token, sx.token, u.products["NIFTY"].spot.token, u.products["SENSEX"].spot.token])
    got = f.ws.sent[0]["inputtoken"].split(",")
    check("subscribe on each contract's segment", got == [f"nse_fo|{ce.token}", f"bse_fo|{sx.token}", "nse_cm|26000", "bse_cm|1"], got)
    from live_trading_options.mcx_terminal import server as S
    e = S.Engine()
    e.universe = u
    e.last[ce.token] = [ce.token, 100.0, 99.5, 100.5, 520, 325, 0, 0, 90.0, 65, 0, 0]
    e.depth[ce.token] = ([(99.5, 520)], [(100.5, 325)])
    q = e.quote(ce.token)
    check("NSE quantities are UNITS (not x lot)", q["bq"] == 520 and q["aq"] == 325 and q["depth"][0][0][1] == 520, q)
    check("seg_of", e.seg_of(sx.token) == "bse_fo" and e.seg_of(26000) == "nse_cm")
    # ATM off the index spot; no spot price yet -> the nearest future
    n = u.products["NIFTY"]
    e.last[n.futures[0].token] = [n.futures[0].token, 24130.0] + [None] * 10
    check("no spot yet -> future", e.und_tick(n, "2026-11-09")[1] == 24130.0)
    e.last[26000] = [26000, 23990.0] + [None] * 10
    check("spot price used", e.und_tick(n, "2026-11-09")[1] == 23990.0)
    e._spot_toks = {26000}
    e.rec = type("R", (), {"tick": lambda *a: None})()
    e.on_ticks([(26000, 0.0, None, None, None, None, None, None, None, None, 0, None, None)])
    check("an index packet with no price keeps the last", e.last[26000][1] == 23990.0, e.last[26000])
    m = e.meta()
    check("meta: desk + spot for the screen", m["desk"] == "NSE" and m["own_book"] and m["hours"]["open"] == "09:15"
          and m["products"][0]["und"]["token"] == 26000, {k: m[k] for k in ("desk", "hours")})


def test_paper_nifty():
    u = universe()
    ce, pe, sx = legs(u)
    q = {ce.token: _q(100.0), sx.token: _q(300.0)}
    path = TMP / "paper.json"
    path.unlink(missing_ok=True)
    b = P.PaperBook(u, lambda t: q.get(t, {}), path=path)
    r = b.place(ce.token, "S", 65, "MKT")
    check("paper short 1 NIFTY lot (65)", r["ok"] and b.pos[ce.token]["net"] == -65, r)
    r = b.place(ce.token, "S", 50, "MKT")
    check("qty must be whole lots", not r["ok"] and "65" in r["msg"], r)
    q[ce.token] = _q(90.0)
    b.place(ce.token, "B", 65, "MKT")
    # sold at the bid 99.50, bought back at the ask 90.50 (paper fills walk the book) -> 9 pts x 65
    check("paper P&L in rupees (points x 65)", abs(b.pos[ce.token]["realized"] - 9.0 * 65) < 1e-6, b.pos[ce.token])
    r = b.place(26000, "B", 65, "MKT")
    check("the index itself cannot be traded", not r["ok"] and "index" in r["msg"], r)
    r = b.place(sx.token, "B", 20, "MKT")
    check("paper SENSEX 1 lot (20)", r["ok"] and b.pos[sx.token]["net"] == 20, r)
    path.unlink(missing_ok=True)


# ── live: OWN BOOK ──────────────────────────────────────────────────────────
def test_own_book_ignores_other_strategies():
    q = {}
    u, b, k, path = _live("other", q)
    ce, pe, sx = legs(u)
    q[ce.token], q[pe.token], q[sx.token] = _q(100.0), _q(80.0), _q(300.0)
    # another strategy on the same account: short 2 lots of the same call + a working order of its own
    k.hand_trade(ce.symbol, "S", 130, 101.0)
    other_working = k.hand_trade(pe.symbol, "S", 65, 85.0, status="open")
    b.sync_positions()
    check("other strategy's positions NOT shown", not any(p.get("net") for p in b.pos.values()), b.pos)
    check("positions readable", b.positions_fresh() and not b.pos_sync["msg"], b.pos_sync)
    # the desk sells 1 lot of the same call
    r = b.place(ce.token, "S", 65, "MKT")
    s = k.sent[-1]
    check("NIFTY order on nse_fo, units", s["exchange_segment"] == "nse_fo" and s["quantity"] == "65", s)
    o = b.orders[-1]
    k.fill(o["broker_id"], 65, 99.0)
    b.poll()
    b.sync_positions()
    p = b.pos[ce.token]
    check("desk position = its own fill only", p["net"] == -65 and abs(p["avg"] - 99.0) < 1e-9, p)
    check("product kept for the exit", p["by_prod"] == {"NRML": -65}, p.get("by_prod"))
    b.poll(); b.sync_positions()
    check("not double counted", b.pos[ce.token]["net"] == -65)
    # SENSEX goes to BSE
    b.place(sx.token, "B", 20, "MKT")
    check("SENSEX order on bse_fo", k.sent[-1]["exchange_segment"] == "bse_fo", k.sent[-1])
    o2 = b.orders[-1]
    k.fill(o2["broker_id"], 20, 301.0)
    b.poll(); b.sync_positions()
    # EXIT ALL: only the desk's own
    k.instant = "complete"
    n0 = len(k.sent)
    r = b.exit_all()
    sent = k.sent[n0:]
    check("EXIT ALL never cancels another strategy's order", other_working not in k.cancels, k.cancels)
    check("EXIT ALL buys back ONLY the desk's 65 (not Kotak's 195)",
          any(x["trading_symbol"] == ce.symbol and x["transaction_type"] == "B" and x["quantity"] == "65" for x in sent)
          and not any(x["trading_symbol"] == ce.symbol and x["quantity"] != "65" for x in sent), sent)
    check("EXIT ALL closes the SENSEX long on bse_fo",
          any(x["trading_symbol"] == sx.symbol and x["transaction_type"] == "S" and x["exchange_segment"] == "bse_fo"
              for x in sent), sent)
    check("EXIT ALL says flat for the desk", r["ok"] and "desk" in r["msg"], r)
    check("desk flat", not any(p.get("net") for p in b.pos.values()), b.pos)
    b.sync_positions()
    check("other strategy still short 130 at Kotak", b.broker_pos[ce.token]["net"] == -130, b.broker_pos.get(ce.token))
    n1 = len(k.sent)
    r = b.exit_all()
    check("second EXIT ALL sends nothing", len(k.sent) == n1 and r["ok"], r)
    path.unlink(missing_ok=True)


def test_own_book_never_bigger_than_kotak():
    """Closed by hand in the Kotak app: the desk must not 'close' it again into a new position."""
    q = {}
    u, b, k, path = _live("handclosed", q)
    ce, pe, sx = legs(u)
    q[pe.token] = _q(80.0)
    k.instant = "complete"
    b.place(pe.token, "S", 130, "MKT")
    b.poll(); b.sync_positions()
    check("desk short 2 lots", b.pos[pe.token]["net"] == -130, b.pos.get(pe.token))
    k.hand_trade(pe.symbol, "B", 65, 70.0)                      # owner buys back 1 lot by hand
    b.sync_positions()
    check("shown: Kotak holds less", "LESS" in b.pos_sync["msg"] and pe.symbol in b.pos_sync["msg"], b.pos_sync)
    r = b.place(pe.token, "B", 130, "MKT")
    check("exit bigger than Kotak holds refused", not r["ok"] and "never bigger" in r["msg"], r)
    n0 = len(k.sent)
    b.exit_all()
    sent = k.sent[n0:]
    check("EXIT ALL buys back only what Kotak still holds (65)", len(sent) == 1 and sent[0]["quantity"] == "65", sent)
    b.sync_positions()
    check("Kotak flat - nothing flipped long", not (b.broker_pos.get(pe.token) or {}).get("net"), b.broker_pos)
    path.unlink(missing_ok=True)


def test_own_book_stop_loss_fill_and_restart():
    from live_trading_options.mcx_terminal import sl as SL
    q = {}
    u, b, k, path = _live("sl", q)
    ce, pe, sx = legs(u)
    q[ce.token] = _q(100.0)
    cfg = {"auto_sl": {"arm_on": "sell", "sl_auto": True, "sl_val": 50, "sl_unit": "%", "qty": "full",
                       "trail_auto": False, "trail_on": False, "trail_every": 10}}
    spath = TMP / "own_sl_state.json"
    spath.unlink(missing_ok=True)
    m = SL.AutoSL(b, lambda t: (q.get(t) or {}).get("ltp"), lambda: cfg, live=True, path=spath, notify=lambda e: None)
    b.before_reduce, b.before_flatten, b.on_push_other, b.sl_ids = m.before_reduce, m.before_flatten, m.on_push, m.working_ids
    k.hand_trade(ce.symbol, "S", 65, 102.0)                    # another strategy's short: never protected by the desk
    k.instant = "complete"
    b.place(ce.token, "S", 65, "MKT")
    b.poll(); b.sync_positions()
    k.instant = None

    def step():
        b.sync_positions()
        m._rows_at = 0.0
        m.step()

    step()
    o = k.sent[-1]
    r = m.recs.get(ce.token)
    check("desk SL on nse_fo for the DESK's 65 only", r and o["order_type"] == "SL" and o["exchange_segment"] == "nse_fo"
          and o["quantity"] == "65" and o["transaction_type"] == "B", (r, o))
    check("SL entry = desk's own average", r and abs(r["entry"] - b.pos[ce.token]["avg"]) < 1e-9, r)
    check("SL id offered to EXIT ALL", m.working_ids() == [r["oid"]], m.working_ids())
    k.fill(r["oid"], 65, 151.0)                                # the stop-loss fills at Kotak
    step()
    check("SL fill closes the desk's position", b.pos[ce.token]["net"] == 0, b.pos[ce.token])
    check("loss booked at the SL price", abs(b.pos[ce.token]["realized"] - (b.orders[-1]["avg"] - 151.0) * 65) < 1e-6,
          b.pos[ce.token])
    step()
    check("SL fill applied once", b.pos[ce.token]["net"] == 0 and b.pos[ce.token]["bought"] == 65, b.pos[ce.token])
    # restart: same book file -> same positions, the SL fill is not applied again
    u2, b2, k2, _ = _live("sl", q, u=u, fake=k, path=path)
    check("restart keeps the desk's positions", b2.pos[ce.token]["net"] == 0 and b2.pos[ce.token]["bought"] == 65, b2.pos)
    check("restart remembers applied SL fills", b2.external_fill(r["oid"], ce.token, "B", 151.0, 65) is False)
    spath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_own_book_cancel_all_only_own():
    q = {}
    u, b, k, path = _live("cancel", q)
    ce, pe, sx = legs(u)
    q[ce.token] = _q(100.0)
    other = k.hand_trade(ce.symbol, "S", 65, 120.0, status="open")
    b.place(ce.token, "S", 65, "LMT", 130.0)                   # desk's resting order
    n = b._cancel_all_broker()
    check("cancel-all = the desk's own working order only", n == 1 and other not in k.cancels
          and b.orders[-1]["broker_id"] in k.cancels, (n, k.cancels))
    path.unlink(missing_ok=True)


def test_own_book_carry_forward():
    """Yesterday's NRML positions of the desk come over; MIS ones (Kotak squared them off) and expired don't."""
    u = universe()
    ce, pe, sx = legs(u)
    d = TMP / "carry"
    d.mkdir(exist_ok=True)
    for f in d.glob("*.json"):
        f.unlink()
    prev = d / "live_book_2026-11-03.json"
    prev.write_text(json.dumps({"orders": [], "positions": {
        str(ce.token): {"token": ce.token, "symbol": ce.symbol, "name": "NIFTY", "lot": 65, "mult": 1.0, "net": -130,
                        "avg": 98.0, "realized": 500.0, "bought": 0, "sold": 130, "by_prod": {"NRML": -65, "MIS": -65}},
        str(pe.token): {"token": pe.token, "symbol": pe.symbol, "name": "NIFTY", "lot": 65, "mult": 1.0, "net": -65,
                        "avg": 70.0, "realized": 0.0, "bought": 0, "sold": 65, "by_prod": {"MIS": -65}},
        "999999": {"token": 999999, "symbol": "NIFTY26OCT...", "name": "NIFTY", "net": -65, "avg": 5.0,
                   "by_prod": {"NRML": -65}}}}), encoding="utf-8")
    today = d / "live_book_2026-11-04.json"
    _, b, k, _ = _live("carry", {}, u=u, path=today, fake=FakeNse(u))
    check("NRML part carried at its average", b.pos.get(ce.token, {}).get("net") == -65 and b.pos[ce.token]["avg"] == 98.0
          and b.pos[ce.token]["realized"] == 0.0, b.pos.get(ce.token))
    check("MIS-only position not carried", not b.pos.get(pe.token), b.pos.get(pe.token))
    check("expired contract dropped", 999999 not in b.pos)
    check("today's file written", today.exists())


def test_mcx_desk_unchanged_flags():
    """The MCX desk keeps mirroring the account (checked in its own process: here only the code paths)."""
    check("MCX profile mirrors", C._DESKS["MCX"]["OWN_BOOK"] is False and C._DESKS["MCX"]["PORT"] == 47680)
    check("MCX desk segments: MCX + NSE commodities (NSE gold)", set(C._DESKS["MCX"]["SEGMENT"].values()) == {"mcx_fo", "nse_com"})
    check("NSE gold not on the NSE index desk", "NSEGOLD" not in C.INSTRUMENTS)


def test_index_stream_by_name():
    """Index prices come as packets 7207 on a Kotak-made id once subscribed BY NAME (checked live 05-Nov)."""
    got = []
    ref = {26000: 24340.0, 26009: 56850.0, 1: 78660.0}
    f = F.KotakFeed(None, got.extend, indices=[("nse_cm", "Nifty 50", 26000), ("nse_cm", "Nifty Bank", 26009),
                                               ("bse_cm", "SENSEX", 1)], ref_price=ref.get)
    f.div = {"nse_cm": 100, "bse_cm": 100}

    class WS:
        sent = []

        def send(self, m):
            self.sent.append(json.loads(m))

    f.ws = WS()
    f._send_indices()
    check("indices subscribed by name", f.ws.sent[-1] == {"event": "subscribeScrips",
                                                          "inputtoken": "nse_cm|Nifty 50,nse_cm|Nifty Bank,bse_cm|SENSEX"},
          f.ws.sent)
    # field order: open, prev close, high, low, LTP, time - the OPEN must never be taken for the price
    pk = lambda ex, kid, ltp, close: F.IDX.pack(F.IDX.size, 7207, ex, 1, 0, 0, 0, kid, ltp + 5000, close, 0, 0, ltp, 1793851136)
    # real values from 05-Nov 09:28: NIFTY 24378.05 (close 23960.60), SENSEX 78910.94 (close 77301.40)
    frame = pk(1, 4247863880, 2437805, 2396060) + pk(5, 3807228871, 7891094, 7730140) + pk(1, 1234567, 5690010, 5591360)
    f._on_message(None, frame)
    t = {x[0]: x for x in got}
    check("NIFTY learned by price", abs(t[26000][1] - 24378.05) < 1e-6 and abs(t[26000][8] - 23960.60) < 1e-6, t.get(26000))
    check("SENSEX = the only BSE index", abs(t[1][1] - 78910.94) < 1e-6, t.get(1))
    check("BANK NIFTY learned by price", abs(t[26009][1] - 56900.10) < 1e-6, t.get(26009))
    check("change vs close", abs(t[26000][11] - (24378.05 - 23960.60)) < 1e-6, t[26000][11])
    got.clear()
    f._on_message(None, pk(1, 4247863880, 2440000, 2396060))
    check("next packet uses the learned id", got and got[0][0] == 26000 and got[0][1] == 24400.0, got)
    f2 = F.KotakFeed(None, lambda x: None, indices=[("nse_cm", "Nifty 50", 26000), ("nse_cm", "Nifty Bank", 26009)],
                     ref_price=lambda tok: None)
    f2.div = {"nse_cm": 100}
    check("two NSE indices and no price known yet: not guessed", f2._index_ticks([(1, 99, 24378.05, 0, 0)]) == [] and not f2.idx_map)
    check("MCX frames unaffected (no index list)", F.decode(pk(1, 5, 100, 100), {}) == [])


if __name__ == "__main__":
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL += 1
            print(f"FAIL {fn.__name__} crashed: {e}")
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
