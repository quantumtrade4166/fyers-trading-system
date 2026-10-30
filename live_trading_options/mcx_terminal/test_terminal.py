"""
mcx_terminal/test_terminal.py — offline tests (no network, no login).

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_terminal
"""
import struct
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.mcx_terminal import audit
from live_trading_options.mcx_terminal import feed as F
from live_trading_options.mcx_terminal import instruments as I

import tempfile as _tf
audit.DIR = Path(_tf.gettempdir()) / "mcx_audit_test"      # tests never write the real audit log

HERE = Path(__file__).resolve().parent
PASS = FAIL = 0


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name} {info}")


def mp_packet(tok, ltp, bid, ask, close, vol=100, oi=50, level=4, exch=9, div=100, nbuy=1, nsell=1):
    body = F.MP.pack(0, 7208, exch, level, 0, 0, 0, tok, 10, 20, vol, 1792397419, 1792397420,
                     int(close * div), int(close * div), int(ltp * div), int(ltp * div), int(ltp * div), 3,
                     int(ltp * div), 0, nbuy, nsell, 0, 125, oi, 0.0, int((ltp - close) * div), 0, 0, 0, 0, 100, 2, 1)
    rows = F.ROW.pack(7, int(bid * div), 2) + F.ROW.pack(9, int(ask * div), 3)
    pkt = body + rows
    return struct.pack("<H", len(pkt)) + pkt[2:]


def test_worked_example():
    # Kotak guide §10: 54-byte mini touch line, nse_cm, divider 100 -> token 11536, LTP 1234.56
    b = bytes.fromhex("3600281c01010007001" "02d0000" "8026b46800000000" "40e20100" "3200000000000000"
                      "90dc0100" "77000000" "b0050000" "01000000" "02" "01000000")
    t = F.decode(b, {"nse_cm": 100})
    check("worked example count", len(t) == 1, t)
    check("worked example token", t[0][0] == 11536, t)
    check("worked example ltp", abs(t[0][1] - 1234.56) < 1e-9, t)
    check("worked example chg", abs(t[0][11] - 14.56) < 1e-9, t)


def test_market_picture_batch():
    frame = mp_packet(582934, 528.9, 527.5, 528.9, 600.0) + mp_packet(583096, 12.3, 12.2, 12.4, 10.0, vol=999, oi=7)
    t = F.decode(frame, {"mcx_fo": 100})
    check("batch two packets", len(t) == 2, t)
    a, b = t
    check("mp token", a[0] == 582934)
    check("mp ltp", abs(a[1] - 528.9) < 1e-6, a)
    check("mp bid/ask", abs(a[2] - 527.5) < 1e-6 and abs(a[3] - 528.9) < 1e-6, a)
    check("mp bid/ask qty", a[4] == 7 and a[5] == 9, a)
    check("mp prev close", abs(a[8] - 600.0) < 1e-6, a)
    check("mp vol/oi", b[6] == 999 and b[7] == 7, b)
    check("mp last update", a[10] == 1792397420, a)


def test_truncated_tail_skipped():
    frame = mp_packet(1, 1.0, 1.0, 1.0, 1.0)
    t = F.decode(frame + frame[:30], {"mcx_fo": 100})
    check("truncated tail", len(t) == 1, t)


def test_depth_level8():
    # level 8: 5 bids + 5 asks -> first bid row and first ask row (after all 5 bids)
    div = 100
    body = mp_packet(5, 100.0, 0, 0, 90.0, level=8, nbuy=5, nsell=5)[:144]
    rows = b"".join(F.ROW.pack(10 + i, int((99 - i) * div), 1) for i in range(5))
    rows += b"".join(F.ROW.pack(20 + i, int((101 + i) * div), 1) for i in range(5))
    pkt = body + rows
    pkt = struct.pack("<H", len(pkt)) + pkt[2:]
    t = F.decode(pkt, {"mcx_fo": 100})[0]
    check("depth best bid", abs(t[2] - 99.0) < 1e-6 and t[4] == 10, t)
    check("depth best ask", abs(t[3] - 101.0) < 1e-6 and t[5] == 20, t)


def test_universe():
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    u = I.parse(text, today="2026-10-19")
    crude = u.products["CRUDEOIL"]
    check("crude lot", crude.lot == 100, crude.lot)
    check("crude opt tick", abs(crude.opt_tick - 0.10) < 1e-9, crude.opt_tick)
    check("crude expiries", crude.expiries()[:2] == ["2026-11-17", "2026-12-16"], crude.expiries())
    ch = crude.chains["2026-11-17"]
    check("crude strike 9300 both legs", 9300.0 in ch and {"CE", "PE"} <= set(ch[9300.0]), list(ch)[:5])
    check("crude ce symbol", ch[9300.0]["CE"].symbol == "CRUDEOIL17NOV269300CE", ch[9300.0]["CE"].symbol)
    check("crude underlying nov opts -> nov fut", crude.underlying("2026-11-17").symbol == "CRUDEOIL19NOV26FUT",
          crude.underlying("2026-11-17").symbol)
    check("no COM rows", all(c.kind in ("FUT", "CE", "PE") for c in u.by_token.values()))
    check("CRUDEOILM excluded", not any(c.name == "CRUDEOILM" for c in u.by_token.values()))
    g = u.products["GOLDM"]
    check("goldm oct opts -> nov fut", g.underlying("2026-10-29").symbol == "GOLDM05NOV26FUT", g.underlying("2026-10-29").symbol)
    s = u.products["SILVERM"]
    check("silverm lot 5", s.lot == 5, s.lot)
    check("expired future dropped", all(f.expiry >= "2026-10-19" for f in crude.futures))


def _book(tmp_name, quotes):
    import tempfile
    from live_trading_options.mcx_terminal import paper as P
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    u = I.parse(text, today="2026-10-19")
    path = Path(tempfile.gettempdir()) / f"mcx_paper_test_{tmp_name}.json"
    path.unlink(missing_ok=True)
    return u, P.PaperBook(u, lambda tok: quotes.get(tok, {}), path=path), path


def test_contract_fields():
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    u = I.parse(text, today="2026-10-19")
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    check("crude freeze 10000", ce.freeze == 10000, ce.freeze)
    check("crude mult 1", ce.mult == 1.0, ce.mult)
    g = next(iter(u.products["GOLDM"].chains["2026-10-29"].values()))["CE"]
    check("goldm mult 0.1", abs(g.mult - 0.1) < 1e-12, g.mult)
    s = next(iter(u.products["SILVERM"].chains.values()))
    s = next(iter(s.values()))["CE"]
    check("silverm freeze 600", s.freeze == 600, s.freeze)


def test_paper_sweep_depth_and_worst_case():
    q = {}
    u, b, path = _book("sweep", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 499.0, "ask": 500.0, "bq": 100, "aq": 100,
                   "depth": ([(499.0, 100), (498.0, 200)], [(500.0, 100), (500.5, 200), (501.0, 300)])}
    r = b.place(ce.token, "B", 1000, "MKT")                  # 10 lots; book has 600 within reach
    check("mkt buy ok", r["ok"], r)
    o = b.orders[-1]
    lim = 505.0                                              # 500 * 1.01 rounded up to the 0.10 tick
    check("mkt limit = ask+1%", abs(o["limit"] - lim) < 1e-9, o["limit"])
    exp_avg = (500 * 100 + 500.5 * 200 + 501 * 300 + lim * 400) / 1000
    check("swept 3 levels + rest at limit", abs(o["avg"] - exp_avg) < 1e-9, (o["avg"], exp_avg))
    check("complete", o["status"] == "COMPLETE" and o["filled"] == 1000, o)
    check("worst case noted", "worst case" in o["reason"], o["reason"])
    p = b.pos[ce.token]
    check("long 1000", p["net"] == 1000 and abs(p["avg"] - exp_avg) < 1e-9, p)
    # sell 4 lots back into bids: 100@499 + 200@498 + 100 at limit (498*0.99... capped by limit)
    r = b.place(ce.token, "S", 400, "MKT")
    o2 = b.orders[-1]
    check("sell limit = bid-1%", abs(o2["limit"] - 494.0) < 1e-9, o2["limit"])
    realized = ((499 - exp_avg) * 100 + (498 - exp_avg) * 200 + (494 - exp_avg) * 100)
    check("realized on partial close", abs(b.pos[ce.token]["realized"] - realized) < 1e-6,
          (b.pos[ce.token]["realized"], realized))
    check("net 600 left", b.pos[ce.token]["net"] == 600)
    check("book saved", path.exists())
    path.unlink(missing_ok=True)


def test_paper_limit_rests_then_fills():
    q = {}
    u, b, path = _book("rest", q)
    pe = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["PE"]
    q[pe.token] = {"ltp": 300.0, "bid": 299.0, "ask": 300.0, "bq": 50, "aq": 50, "depth": None}
    r = b.place(pe.token, "S", 300, "LMT", 302.0)            # sell 3 lots at 302: above the bid, rests
    o = b.orders[-1]
    check("lmt rests", r["ok"] and o["status"] == "OPEN" and o["filled"] == 0, o)
    q[pe.token] = {"ltp": 302.5, "bid": 302.5, "ask": 303.0, "bq": 200, "aq": 50, "depth": None}
    b.on_tick(pe.token)
    check("filled 2 lots at the bid when it reached the limit", o["filled"] == 200 and o["avg"] == 302.5, o)
    q[pe.token] = {"ltp": 303.0, "bid": 303.0, "ask": 303.5, "bq": 500, "aq": 50, "depth": None}
    b.on_tick(pe.token)
    check("rest filled", o["status"] == "COMPLETE" and o["filled"] == 300, o)
    check("short 300", b.pos[pe.token]["net"] == -300)
    path.unlink(missing_ok=True)


def test_paper_freeze_split_and_caps():
    q = {}
    u, b, path = _book("split", q)
    s = u.products["SILVERM"]
    ex = s.expiries()[0]
    k = sorted(s.chains[ex])[len(s.chains[ex]) // 2]
    c = s.chains[ex][k]["CE"]                                # freeze 600 units = 120 lots of 5
    q[c.token] = {"ltp": 4000.0, "bid": 3990.0, "ask": 4000.0, "bq": 5000, "aq": 5000, "depth": None}
    r = b.place(c.token, "S", 250 * 5, "MKT")                # 250 lots -> 120 + 120 + 10
    check("split ok", r["ok"] and len(r["ids"]) == 3, r)
    check("slice sizes", [o["qty"] for o in b.orders[-3:]] == [600, 600, 50], [o["qty"] for o in b.orders[-3:]])
    check("slices share the book", sum(o["filled"] for o in b.orders[-3:]) == 1250)
    r = b.place(c.token, "B", 301 * 5, "MKT")
    check("cap refused", not r["ok"] and "cap" in r["msg"], r)
    r = b.place(c.token, "B", 7, "MKT")
    check("non-lot qty refused", not r["ok"], r)
    r = b.place(c.token, "B", 5, "LMT", None)
    check("lmt needs price", not r["ok"], r)
    path.unlink(missing_ok=True)


def test_paper_goldm_pnl_and_flip_and_exit():
    q = {}
    u, b, path = _book("goldm", q)
    g = u.products["GOLDM"].chains["2026-10-29"]
    c = g[sorted(g)[len(g) // 2]]["PE"]
    q[c.token] = {"ltp": 2000.0, "bid": 2000.0, "ask": 2001.0, "bq": 10000, "aq": 10000, "depth": None}
    b.place(c.token, "S", 500, "MKT")                        # short 5 lots at 2000
    q[c.token] = {"ltp": 1900.0, "bid": 1899.0, "ask": 1900.0, "bq": 10000, "aq": 10000, "depth": None}
    b.place(c.token, "B", 800, "MKT")                        # cover 500 at 1900 + go long 300
    p = b.pos[c.token]
    check("goldm realized = 100 pts x 500 x 0.1", abs(p["realized"] - 5000.0) < 1e-6, p["realized"])
    check("flipped long 300 at 1900", p["net"] == 300 and p["avg"] == 1900.0, p)
    r = b.exit_all()
    check("exit all flat", r["ok"] and b.pos[c.token]["net"] == 0, (r, b.pos[c.token]))
    path.unlink(missing_ok=True)


def test_engine_quote_lots_to_units():
    from live_trading_options.mcx_terminal.server import Engine
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    e = Engine()
    e.universe = I.parse(text, today="2026-10-19")
    g = e.universe.products["GOLDM"].chains["2026-10-29"]
    c = g[sorted(g)[len(g) // 2]]["CE"]
    e.last[c.token] = [c.token, 1640.0, 1638.0, 1644.5, 3, 2, 5000, 1600, 1700.0, 1, 0, -60.0]
    e.depth[c.token] = ([(1638.0, 3), (1637.5, 1)], [(1644.5, 2), (1645.0, 4)])
    q = e.quote(c.token)
    check("touch qty lots->units", q["bq"] == 300 and q["aq"] == 200, q)
    check("depth qty lots->units", q["depth"][0][0] == (1638.0, 300) and q["depth"][1][1] == (1645.0, 400), q["depth"])


def test_risk_guard_stop_trail_target():
    import tempfile
    from live_trading_options.mcx_terminal.risk import RiskGuard
    q = {}
    u, b, path = _book("risk", q)
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    q[ce.token] = {"ltp": 500.0, "bid": 500.0, "ask": 500.1, "bq": 10**6, "aq": 10**6, "depth": None}
    b.place(ce.token, "S", 100, "MKT")                         # short 1 lot at 500
    ltp = {ce.token: 500.0}
    rpath = Path(tempfile.gettempdir()) / "mcx_risk_test.json"
    rpath.unlink(missing_ok=True)
    g = RiskGuard(b, lambda t: ltp.get(t), path=rpath)
    r = g.set(stop=-2000, target=None, trail=1000)
    check("risk armed", r["ok"] and g.state()["armed"], r)
    ltp[ce.token] = 485.0                                      # +1500 -> one trail step
    g.check()
    check("trail raised stop by one step", g.state()["stop"] == -1000.0, g.state())
    ltp[ce.token] = 498.0                                      # +200, above the stop
    check("no fire above stop", g.check() is None and b.pos[ce.token]["net"] == -100)
    ltp[ce.token] = 511.0                                      # -1100 <= -1000 -> fire
    q[ce.token] = {"ltp": 511.0, "bid": 510.9, "ask": 511.0, "bq": 10**6, "aq": 10**6, "depth": None}
    reason = g.check()
    check("first reading over the stop only starts the confirm", reason is None and b.pos[ce.token]["net"] == -100,
          reason)
    g._breach = (g._breach[0], g._breach[1] - 2.0)          # CONFIRM_S later, still over the line
    reason = g.check()
    check("stop fired", reason is not None and "stop-loss" in reason, reason)
    check("flat after fire", b.pos[ce.token]["net"] == 0, b.pos[ce.token])
    check("disarmed after fire", not g.state()["armed"] and g.state()["fired"])
    r = g.set(stop=None, target=None, trail=None)
    check("set needs stop or target", not r["ok"])
    r = g.set(stop=10**9)
    check("stop above current P&L refused", not r["ok"], r)
    rpath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_risk_guard_ignores_half_loaded_prices():
    """30-Oct VPS: right after a restart only the LOSING legs had prices; legs with no price were left
    out of the P&L, so it read -18,150 (real +5,950) and the -15,000 guard squared off all 5 positions."""
    import tempfile
    from live_trading_options.mcx_terminal.risk import RiskGuard
    q = {}
    u, b, path = _book("riskhalf", q)
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    win, lose = ch[9300.0]["CE"], ch[9300.0]["PE"]
    for t in (win, lose):
        q[t.token] = {"ltp": 500.0, "bid": 500.0, "ask": 500.1, "bq": 10**6, "aq": 10**6, "depth": None}
        b.place(t.token, "S", 100, "MKT")                      # short both at 500
    ltp = {win.token: 300.0, lose.token: 600.0}               # real P&L: +200*100 -100*100 = +10,000
    rpath = Path(tempfile.gettempdir()) / "mcx_risk_half_test.json"
    rpath.unlink(missing_ok=True)
    g = RiskGuard(b, lambda t: ltp.get(t), path=rpath)
    check("guard set on full prices", g.set(stop=-5000)["ok"])
    ltp.pop(win.token)                                        # restart: the winning leg has no price yet
    for _ in range(3):
        g._breach = None if g._breach is None else (g._breach[0], g._breach[1] - 5)
        g.check()
    check("no fire on a half-loaded P&L", g.state()["armed"] and b.pos[win.token]["net"] == -100
          and b.pos[lose.token]["net"] == -100, g.state())
    check("half-loaded P&L not shown as the guard's P&L", g.state()["pnl"] != -10000.0, g.state())
    g.clear()
    r = g.set(stop=-5000)
    check("cannot set the guard while a price is missing", not r["ok"] and "no price" in r["msg"], r)
    ltp[win.token] = 300.0
    check("prices back: set works", g.set(stop=-5000)["ok"])
    # a single bad reading (one tick) over the stop does not fire; a held one does
    ltp[lose.token] = 800.0                                   # -30000 +20000 = -10,000 <= -5,000
    check("one reading over the stop: not yet", g.check() is None and g.state()["armed"])
    ltp[lose.token] = 600.0
    check("back above: confirm reset", g.check() is None and g._breach is None)
    rpath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_basket_buys_first_and_move():
    q = {}
    u, b, path = _book("basket", q)
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    ks = sorted(ch)
    i = ks.index(9300.0)
    for k in ks[i - 3:i + 4]:
        for leg in ch[k].values():
            q[leg.token] = {"ltp": 100.0, "bid": 99.9, "ask": 100.0, "bq": 10**6, "aq": 10**6, "depth": None}
    sell_ce, hedge_ce = ch[9300.0]["CE"], ch[ks[i + 2]]["CE"]
    r = b.basket([{"token": sell_ce.token, "side": "S", "qty": 200}, {"token": hedge_ce.token, "side": "B", "qty": 200}])
    check("basket ok", r["ok"] and len(r["ids"]) == 2, r)
    check("hedge bought first", b.orders[-2]["token"] == hedge_ce.token and b.orders[-2]["side"] == "B",
          [(o["symbol"], o["side"]) for o in b.orders[-2:]])
    r = b.move(sell_ce.token, 1, away=True)                    # short CE 9300 -> 9350
    check("move ok", r["ok"], r)
    check("old leg flat", b.pos[sell_ce.token]["net"] == 0)
    check("new leg short 2 lots", b.pos[ch[ks[i + 1]]["CE"].token]["net"] == -200)
    r = b.move(ch[ks[i + 1]]["CE"].token, 1, away=False)      # back toward ATM
    check("move close ok", r["ok"] and b.pos[sell_ce.token]["net"] == -200, r)
    path.unlink(missing_ok=True)


def test_strike_gap_setting_and_move():
    import tempfile
    from live_trading_options.mcx_terminal.settings import Settings
    spath = Path(tempfile.gettempdir()) / "mcx_settings_gap.json"
    spath.write_text('{"strike_gap": {"CRUDEOIL": 2, "GOLDM": 1, "SILVERM": 1}}', encoding="utf-8")
    S = Settings(path=spath)
    g = S.get()["strike_gap"]
    check("old 'every 2nd' crude -> 100 pts", g["CRUDEOIL"] == 100, g)
    check("old 1 -> native gap", g["GOLDM"] == 500 and g["SILVERM"] == 1000 and g["GOLD"] == 500, g)
    check("allowed choice saved", S.update({"strike_gap": {"SILVERM": 4000}})["ok"] and S.get()["strike_gap"]["SILVERM"] == 4000)
    S.update({"strike_gap": {"CRUDEOIL": 150, "NATURALGAS": 5}})
    check("value not in the list ignored", S.get()["strike_gap"]["CRUDEOIL"] == 100 and "NATURALGAS" not in S.get()["strike_gap"])
    spath.unlink(missing_ok=True)
    # Move by 1 = one strike GAP (100 pts), not one listed strike (50 pts)
    q = {}
    u, b, path = _book("gapmove", q)
    b.settings = S.get
    ch = u.products["CRUDEOIL"].chains["2026-11-17"]
    for k in (9200.0, 9300.0, 9400.0, 9500.0):
        for leg in ch[k].values():
            q[leg.token] = {"ltp": 100.0, "bid": 99.9, "ask": 100.0, "bq": 10**6, "aq": 10**6, "depth": None}
    ce = ch[9300.0]["CE"]
    b.place(ce.token, "S", 100, "MKT")
    r = b.move(ce.token, 1, away=True)
    check("move away 1 gap: 9300 -> 9400 CE", r["ok"] and b.pos[ch[9400.0]["CE"].token]["net"] == -100, r)
    r = b.move(ch[9400.0]["CE"].token, 50, away=True)
    check("too far refused with the strike named", not r["ok"] and "14400" in r["msg"], r)
    path.unlink(missing_ok=True)


def test_paper_auto_stop_loss():
    import tempfile
    from live_trading_options.mcx_terminal import sl as SL
    q = {}
    u, b, path = _book("papersl", q)
    pe = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["PE"]
    q[pe.token] = {"ltp": 200.0, "bid": 199.9, "ask": 200.0, "bq": 10**6, "aq": 10**6, "depth": None}
    cfg = {"auto_sl": {"arm_on": "all", "sl_auto": True, "sl_val": 20, "sl_unit": "pts", "qty": "full"}}
    spath = Path(tempfile.gettempdir()) / "mcx_paper_sl.json"
    spath.unlink(missing_ok=True)
    m = SL.AutoSL(b, lambda t: q[t]["ltp"], lambda: cfg, live=False, path=spath)
    b.before_reduce = m.before_reduce
    b.place(pe.token, "B", 100, "MKT")
    m.step()
    r = m.recs.get(pe.token)
    check("paper long watched, stop 20 pts below entry", r and r["state"] == "watching" and r["trigger"] == r["entry"] - 20, r)
    q[pe.token] = {"ltp": 170.0, "bid": 169.9, "ask": 170.0, "bq": 10**6, "aq": 10**6, "depth": None}
    m.step()
    check("paper stop hit -> flat", not b.pos[pe.token]["net"], b.pos.get(pe.token))
    m.step()
    check("no re-arm after the hit", pe.token not in m.recs)
    spath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_settings_pricing_reprice_and_push():
    import tempfile
    from live_trading_options.mcx_terminal.settings import Settings
    spath = Path(tempfile.gettempdir()) / "mcx_settings_test.json"
    spath.unlink(missing_ok=True)
    S = Settings(path=spath)
    r = S.update({"entry_ref": "ltp", "entry_buffer": 0, "exit_ref": "touch", "exit_buffer": 2, "exit_unit": "pts",
                  "reprice": True, "market_after_s": 0})
    check("settings saved", r["ok"] and spath.exists(), r)
    check("bad choice ignored", S.update({"entry_ref": "nonsense"})["ok"] and S.get()["entry_ref"] == "ltp")
    check("huge % buffer refused", not S.update({"entry_buffer": 50, "entry_unit": "%"})["ok"])
    q = {}
    u, b, path = _book("settings", q)
    b.settings = S.get
    pe = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["PE"]
    # entry at LTP, no buffer: ltp 300 is below the ask 301 -> the buy waits (book beyond the limit is visible)
    q[pe.token] = {"ltp": 300.0, "bid": 299.5, "ask": 301.0, "bq": 1000, "aq": 1000,
                   "depth": ([(299.5, 1000)], [(301.0, 1000), (302.0, 1000)])}
    r = b.place(pe.token, "B", 100, "MKT")
    o = b.orders[-1]
    check("ltp entry limit", o["limit"] == 300.0 and o["role"] == "entry", o)
    check("waits when the book is past the limit", o["status"] == "OPEN" and o["filled"] == 0, o)
    # the ask comes down to the LTP; a re-price round fills it
    q[pe.token] = {"ltp": 300.5, "bid": 300.0, "ask": 300.5, "bq": 1000, "aq": 1000,
                   "depth": ([(300.0, 1000)], [(300.5, 1000)])}
    o["t_rep"] = 0
    b.housekeep()
    check("re-priced to new ltp and filled", o["status"] == "COMPLETE" and o["avg"] == 300.5, o)
    # exit uses touch - 2 pts
    b.place(pe.token, "S", 100, "MKT")
    o2 = b.orders[-1]
    check("exit role + touch - 2 pts", o2["role"] == "exit" and abs(o2["limit"] - 298.0) < 1e-9, o2)
    # push-to-market: a typed limit far away is forced through after N seconds
    S.update({"market_after_s": 0.5})
    q[pe.token] = {"ltp": 300.0, "bid": 299.0, "ask": 300.0, "bq": 1000, "aq": 1000, "depth": None}
    b.place(pe.token, "B", 100, "LMT", 250.0)
    o3 = b.orders[-1]
    check("typed limit waits", o3["status"] == "OPEN")
    o3["t0"] -= 1
    b.housekeep()
    check("pushed to market", o3["status"] == "COMPLETE" and o3["avg"] == 300.0 and "pushed" in o3["reason"], o3)
    spath.unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def test_mtm_history_buckets():
    import tempfile
    from live_trading_options.mcx_terminal.mtm import MtmHistory
    mpath = Path(tempfile.gettempdir()) / "mcx_mtm_test.json"
    mpath.unlink(missing_ok=True)
    h = MtmHistory(path=mpath)
    pos = [{"token": 1, "name": "CRUDEOIL", "net": -100, "avg": 500.0, "mult": 1.0, "realized": 0.0},
           {"token": 2, "name": "GOLDM", "net": 0, "avg": 0.0, "mult": 0.1, "realized": 250.0}]
    t0 = 1_800_000_000 - (1_800_000_000 % 60)
    for i, px in enumerate([500, 498, 495, 497, 490]):        # one sample every 20 s
        h.sample(pos, lambda t, px=px: px if t == 1 else None, now=t0 + i * 20)
    r = h.query(1, "all")
    check("1-min buckets keep last sample", [p[1] for p in r["points"]] == [750.0, 1250.0], r["points"])
    check("instruments listed", r["instruments"] == ["CRUDEOIL", "GOLDM"])
    r = h.query(1, "GOLDM")
    check("per-instrument curve", [p[1] for p in r["points"]] == [250.0, 250.0], r["points"])
    check("saved", mpath.exists())
    mpath.unlink(missing_ok=True)


def test_access_guard():
    import asyncio
    import time as _t
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from live_trading_options.mcx_terminal.access import AccessGuard
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _K:
        def get_signing_key_from_jwt(self, t):
            return type("K", (), {"key": key.public_key()})()

    hits = []

    async def inner(scope, receive, send):
        hits.append(scope["path"])

    def run(g, headers, typ="http", path="/"):
        sent = []

        async def send(m):
            sent.append(m)
        scope = {"type": typ, "path": path, "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
        asyncio.run(g(scope, None, send))
        return sent

    def tok(aud="AUD1", email="me@x.com", exp=3600, iss="https://team1.cloudflareaccess.com"):
        return jwt.encode({"aud": aud, "email": email, "exp": int(_t.time()) + exp, "iss": iss}, key, algorithm="RS256")

    g = AccessGuard(inner, team="team1", aud="AUD1", emails="me@x.com")
    g._keys = lambda: _K()
    hits.clear()
    run(g, {"host": "localhost:47680"})
    check("local (ssh tunnel) allowed", hits == ["/"], hits)
    s = run(g, {"host": "mcx.trading.contact", "cf-ray": "x"})
    check("external without token refused", s and s[0].get("status") == 403 and len(hits) == 1, s)
    run(g, {"host": "mcx.trading.contact", "cf-ray": "x", "cf-access-jwt-assertion": tok()})
    check("valid token allowed", len(hits) == 2, hits)
    run(g, {"host": "mcx.trading.contact", "cookie": "a=1; CF_Authorization=" + tok()})
    check("valid cookie allowed", len(hits) == 3, hits)
    s = run(g, {"host": "mcx.trading.contact", "cf-access-jwt-assertion": tok(aud="OTHER")})
    check("wrong audience refused", s[0].get("status") == 403 and len(hits) == 3, s)
    s = run(g, {"host": "mcx.trading.contact", "cf-access-jwt-assertion": tok(email="evil@x.com")})
    check("other email refused", s[0].get("status") == 403 and len(hits) == 3, s)
    s = run(g, {"host": "mcx.trading.contact", "cf-access-jwt-assertion": tok(exp=-60)})
    check("expired token refused", s[0].get("status") == 403 and len(hits) == 3, s)
    s = run(g, {"host": "localhost", "cf-ray": "x"})
    check("cloudflare header with local host still external", s[0].get("status") == 403, s)
    s = run(g, {"host": "mcx.trading.contact", "cf-ray": "x"}, typ="websocket", path="/ws")
    check("websocket refused with 4403", s == [{"type": "websocket.close", "code": 4403}], s)
    g2 = AccessGuard(inner, team="", aud="", emails="")
    s = run(g2, {"host": "mcx.trading.contact", "cf-access-jwt-assertion": tok()})
    check("fail closed when not configured", s[0].get("status") == 403, s)


def test_carry_forward_overnight():
    import json as _j
    import tempfile
    from live_trading_options.mcx_terminal import paper as P
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    u = I.parse(text, today="2026-10-19")
    d = Path(tempfile.mkdtemp())
    ce = u.products["CRUDEOIL"].chains["2026-11-17"][9300.0]["CE"]
    (d / "paper_book_2026-10-22.json").write_text(_j.dumps({"orders": [], "positions": {
        str(ce.token): {"token": ce.token, "symbol": ce.symbol, "name": "CRUDEOIL", "lot": 100, "mult": 1.0,
                        "net": -200, "avg": 480.0, "realized": 1234.0, "bought": 0, "sold": 200},
        "999999": {"token": 999999, "symbol": "OLDEXPIRED", "name": "CRUDEOIL", "lot": 100, "mult": 1.0,
                   "net": -100, "avg": 10.0, "realized": 0.0, "bought": 0, "sold": 100}}}), encoding="utf-8")
    from live_trading_options.mcx_terminal import config as _C
    off = P.PaperBook(u, lambda t: {}, path=d / "paper_book_2026-10-24.json")
    check("default: every day starts clean (no carry)", not _C.PAPER_CARRY and not off.pos, off.pos)
    _C.PAPER_CARRY = True
    try:
        b = P.PaperBook(u, lambda t: {}, path=d / "paper_book_2026-10-23.json")
    finally:
        _C.PAPER_CARRY = False
    p = b.pos.get(ce.token)
    check("open position carried", p and p["net"] == -200 and p["avg"] == 480.0 and p["carried"], p)
    check("realized reset for the new day", p and p["realized"] == 0.0, p)
    check("expired contract dropped", 999999 not in b.pos)
    check("new day saved", (d / "paper_book_2026-10-23.json").exists())


def test_recorder_ring_activation_roundtrip():
    import tempfile
    from live_trading_options.mcx_terminal import recorder as R
    d = Path(tempfile.mkdtemp())
    rec = R.DayRecorder(day="2026-10-26", folder=d)
    rec._stop.set()                                            # drive flushes by hand
    for i in range(1000):
        rec.tick((1000 + i, 5, 1.0 + i, None, None, None, None, None, None, None, None, None, None))
    rec.event("book", {"t": "book", "positions": [], "orders": []})
    rec.flush()
    check("no file before the first order", not rec.path.exists())
    rec.activate(lambda: {"meta": {"x": 1}})
    for i in range(500):
        rec.tick((5000 + i, 6, 2.0, None, None, None, None, None, None, None, None, None, None))
    rec.flush()
    check("file after the first order", rec.path.exists())
    ticks, events = R.read_raw(rec.path)
    check("ring (before) + after all kept, in order", len(ticks) == 1500 and ticks[0][0] == 1000 and ticks[-1][0] == 5499,
          (len(ticks), ticks[:1], ticks[-1:]))
    check("header recorded", any(e[1] == "header" and e[2] == {"meta": {"x": 1}} for e in events), events[:3])
    check("events kept", any(e[1] == "book" for e in events))
    rec2 = R.DayRecorder(day="2026-10-26", folder=d)
    rec2._stop.set()
    check("restart mid-day keeps appending", rec2.active)


def test_engine_records_only_visible_changes():
    import tempfile
    from live_trading_options.mcx_terminal import recorder as R
    from live_trading_options.mcx_terminal.server import Engine
    e = Engine()
    e.rec = R.DayRecorder(day="2099-01-02", folder=Path(tempfile.mkdtemp()))
    e.rec._stop.set()
    t = (7, 100.0, 99.5, 100.5, 3, 2, 10, 5, 98.0, 1, 1_792_000_000, 2.0, None)
    e.on_ticks([t])
    e.on_ticks([t])                                            # identical repeat (e.g. a depth-only packet)
    e.on_ticks([(7, 100.0, 99.5, 100.5, 3, 2, 10, 5, 98.0, 1, 1_792_000_000, 2.0, ([(99.5, 3)], [(100.5, 2)]))])
    e.on_ticks([(7, 101.0, 100.5, 101.5, 3, 2, 11, 5, 98.0, 1, 1_792_000_001, 3.0, None)])
    check("repeats not recorded", len(e.rec.ticks) == 2, len(e.rec.ticks))


def test_replay_builder_file():
    import tempfile
    import base64
    import gzip
    import json as _j
    from live_trading_options.mcx_terminal import recorder as R
    from live_trading_options.mcx_terminal import replay_build as B
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    u = I.parse(text, today="2026-10-19")
    p = u.products["CRUDEOIL"]
    ch = p.chains["2026-11-17"]
    rows = [{"k": k, "ce": l["CE"].token, "pe": l["PE"].token, "cs": l["CE"].symbol, "ps": l["PE"].symbol} for k, l in ch.items()]
    fut = p.underlying("2026-11-17")
    cdef = {"t": "chain", "name": "CRUDEOIL", "expiry": "2026-11-17", "lot": 100, "tick": 0.1,
            "fut": {"token": fut.token, "symbol": fut.symbol}, "rows": rows}
    d = Path(tempfile.mkdtemp())
    rec = R.DayRecorder(day="2026-10-27", folder=d)
    rec._stop.set()
    ce = rows[10]["ce"]
    rec.activate(lambda: {"meta": {"products": [{"name": "CRUDEOIL", "futures": [{"token": fut.token}]}]},
                          "chains": [cdef], "last": [[ce, 400.0, None, None, None, None, None, None, None, None, None, None]]})
    t0 = 1_792_000_000_000
    for i in range(300):
        rec.tick((t0 + i * 100, ce, 400.0 + i * 0.1, 399.9, 400.1, 3, 2, 100 + i, 50, 410.0, 1, 1_792_000_000 + i // 10, -10.0))
        rec.tick((t0 + i * 100, 123456789, 1.0, None, None, None, None, None, None, None, None, None, None))  # not on screen
    rec.events.append((t0 + 5000, "book", {"t": "book", "positions": [], "orders": [
        {"id": 1, "token": ce, "symbol": "X", "name": "CRUDEOIL", "side": "S", "qty": 100, "lot": 100}]}))
    rec.flush()
    out = B.build("2026-10-27", folder=d)
    html = out.read_text(encoding="utf-8")
    check("html built", out.exists() and "rp-ticks" in html and "ReplaySocket" in html and "function onChain" in html)
    info = _j.loads(html.split('id="rp-info" type="application/json">')[1].split("</script>")[0])
    check("off-screen contract dropped", info["n"] == 300 and 123456789 not in info["tokens"], info["n"])
    blob = gzip.decompress(base64.b64decode(html.split('id="rp-ticks" type="application/octet-stream">')[1].split("</script>")[0]))
    import numpy as np
    n = info["n"]
    ltp = np.frombuffer(blob[6 * n: 6 * n + 4 * n], dtype=np.float32)
    check("float32 prices exact to the tick", abs(float(ltp[-1]) - 429.9) < 1e-3, float(ltp[-1]))


def test_mtm_imbalance_series():
    import tempfile
    from live_trading_options.mcx_terminal.mtm import MtmHistory
    mpath = Path(tempfile.gettempdir()) / "mcx_mtm_imb_test.json"
    mpath.unlink(missing_ok=True)
    h = MtmHistory(path=mpath)
    pos = [{"token": 1, "symbol": "CRUDEOIL17NOV269500CE", "name": "CRUDEOIL", "net": -200, "avg": 300.0, "mult": 1.0, "realized": 0.0},
           {"token": 2, "symbol": "CRUDEOIL17NOV269300PE", "name": "CRUDEOIL", "net": -100, "avg": 200.0, "mult": 1.0, "realized": 0.0},
           {"token": 3, "symbol": "CRUDEOIL17NOV269900CE", "name": "CRUDEOIL", "net": 200, "avg": 50.0, "mult": 1.0, "realized": 0.0}]
    px = {1: 300.0, 2: 200.0, 3: 50.0}
    t0 = 1_800_000_000 - (1_800_000_000 % 60)
    h.sample(pos, px.get, now=t0)
    r = h.query(1, "all", "imb")
    # short CE premium 300*200 = 60,000 (the long hedge is not counted) · short PE 200*100 = 20,000
    check("imbalance % = (CE-PE)/(CE+PE)", r["points"] and abs(r["points"][0][1] - 50.0) < 1e-9, r["points"])
    check("ce/pe premium carried", r["points"][0][2:] == [60000.0, 20000.0], r["points"])
    h.sample([pos[2]], px.get, now=t0 + 60)                   # only a long left -> no short premium
    check("no point when nothing is short", len(h.query(1, "all", "imb")["points"]) == 1)
    mpath.unlink(missing_ok=True)


def test_atm_straddle():
    from live_trading_options.mcx_terminal.server import Engine
    text = (HERE / "testdata" / "mcx_scrip_sample.csv").read_text(encoding="utf-8")
    e = Engine()
    e.universe = I.parse(text, today="2026-10-19")
    p = e.universe.products["CRUDEOIL"]
    exp = p.expiries()[0]
    fut = p.underlying(exp)
    e.last[fut.token] = [fut.token, 9312.0] + [None] * 10
    ch = p.chains[exp]
    e.last[ch[9300.0]["CE"].token] = [ch[9300.0]["CE"].token, 410.5] + [None] * 10
    e.last[ch[9300.0]["PE"].token] = [ch[9300.0]["PE"].token, 398.0] + [None] * 10
    v = e.atm_straddles()
    check("ATM strike nearest the future", v.get("CRUDEOIL", [None])[0] == 9300.0, v)
    check("ATM straddle = CE + PE", abs(v["CRUDEOIL"][1] - 808.5) < 1e-9, v)
    check("no value without prices", "GOLDM" not in v)


def test_atm_history():
    import tempfile
    from live_trading_options.mcx_terminal.mtm import AtmHistory
    p = Path(tempfile.gettempdir()) / "mcx_atm_test.json"
    p.unlink(missing_ok=True)
    h = AtmHistory(path=p)
    t0 = 1_800_000_000 - (1_800_000_000 % 60)
    for i, (k, v) in enumerate([(9300, 800.0), (9300, 790.0), (9350, 780.0), (9350, 775.0)]):
        h.sample({"CRUDEOIL": [k, v, "2026-11-17"], "GOLDM": [140500, 3100.0 + i, "2026-10-29"]}, now=t0 + i * 20)
    r = h.query("CRUDEOIL", 1)
    check("atm buckets keep last + strike", r["points"] == [[t0 + 40, 780.0, 9350], [t0 + 60, 775.0, 9350]], r["points"])
    check("atm per instrument", len(h.query("GOLDM", 0.1)["points"]) == 4)
    check("atm saved", p.exists())
    p.unlink(missing_ok=True)


if __name__ == "__main__":
    for fn in [v for k, v in dict(globals()).items() if k.startswith("test_")]:
        fn()
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
