"""
mcx_terminal/test_btc.py — offline tests for the BTC DESK (Delta Exchange India BTC options, PAPER only).
No network, no key, no order can be sent from here.

The BTC desk is the MCX terminal with TERMINAL_DESK=BTC. What is different and tested here:
  * contracts from Delta's product list (product id = token), options priced off the BTC index
  * expiry = IST date of the 17:30 IST settlement; settled / non-BTC rows dropped
  * Delta's public feed messages -> the terminal's tick tuples (l1 book, ticker, l2 depth, index)
  * money in USD: 1 contract = 0.001 BTC -> P&L = points x contracts x 0.001
  * the desk's day runs 17:35 -> 17:35 IST and is named by the date it ends on
  * no live orders: the live book refuses everything

    .venv\\Scripts\\python.exe -m live_trading_options.mcx_terminal.test_btc
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TERMINAL_DESK"] = "BTC"                     # before anything reads config.py
sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal import delta_instruments as D
from live_trading_options.mcx_terminal import delta_feed as F
from live_trading_options.mcx_terminal import paper as P
from live_trading_options.mcx_terminal.nolive import NoLiveBook

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.gettempdir()) / "btc_desk_test"
TMP.mkdir(exist_ok=True)
PASS = FAIL = 0
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime(2026, 11, 15, 1, 0, tzinfo=dt.timezone.utc)      # 06:30 IST 15-Nov: the 15-Nov daily still live


def check(name, cond, info=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"FAIL {name} {info}")


def universe():
    rows = json.loads((HERE / "testdata" / "delta_products_sample.json").read_text(encoding="utf-8"))
    return D.parse(rows, now=NOW)


def test_config():
    check("desk", C.DESK == "BTC")
    check("port", C.PORT == 47684)
    check("own data folder", C.DATA.name == "data_btc")
    check("paper only", C.LIVE_ORDERS is False)
    check("usd", C.CCY == "$")
    check("reset after expiry", C.RESET_AT == dt.time(17, 35))
    check("24/7", C.HOURS.get("always") is True)
    check("btc url", C.DESK_URLS["BTC"].startswith("https://btc."))


def test_day():
    d = lambda h, m, day=14: C.day(dt.datetime(2026, 11, day, h, m, tzinfo=IST))
    check("before 17:35 = same date", d(17, 34) == "2026-11-14", d(17, 34))
    check("at 17:35 = next date", d(17, 35) == "2026-11-15", d(17, 35))
    check("evening = next date", d(23, 59) == "2026-11-15")
    check("after midnight = that date", d(0, 1, 15) == "2026-11-15")
    check("next afternoon still that day", d(17, 29, 15) == "2026-11-15")


def test_parse():
    u = universe()
    p = u.products["BTC"]
    check("two expiries", p.expiries() == ["2026-11-15", "2026-11-16"], p.expiries())
    check("settled dropped", 1 not in u.by_token)
    check("ETH ignored", 2 not in u.by_token)
    ch = p.chains["2026-11-15"]
    k = min(ch, key=lambda s: abs(s - 65000))
    ce, pe = ch[k]["CE"], ch[k]["PE"]
    check("call / put", ce.symbol.startswith("C-BTC-") and pe.symbol.startswith("P-BTC-"), (ce.symbol, pe.symbol))
    check("strike from symbol", str(int(k)) in ce.symbol)
    check("lot 1 contract", ce.lot == 1 and p.lot == 1)
    check("mult 0.001 USD/pt", abs(ce.mult - 0.001) < 1e-12, ce.mult)
    check("tick 0.1", abs(ce.tick - 0.1) < 1e-9, ce.tick)
    check("segment delta", ce.seg == "delta")
    check("underlying = index", p.underlying("2026-11-15").token == C.BTC_SPOT_TOKEN)
    check("index in tokens", u.by_token[C.BTC_SPOT_TOKEN].kind == "IDX")
    # after 17:30 IST on 15-Nov the 15-Nov daily is gone
    rows = json.loads((HERE / "testdata" / "delta_products_sample.json").read_text(encoding="utf-8"))
    late = D.parse(rows, now=dt.datetime(2026, 11, 15, 12, 0, 1, tzinfo=dt.timezone.utc))
    check("expired daily dropped at 17:30 IST", late.products["BTC"].expiries() == ["2026-11-16"],
          late.products["BTC"].expiries())


def test_feed_convert():
    u = universe()
    ce = next(c for c in u.by_token.values() if c.kind == "CE")
    tok_of = {ce.symbol: ce.token}
    sp = F.convert({"type": "spot_price", "symbol": ".DEXBTUSD", "price": 65248.9}, tok_of, C.BTC_SPOT_TOKEN)
    check("index tick", sp and sp[0][0] == C.BTC_SPOT_TOKEN and sp[0][1] == 65248.9, sp)
    check("other index ignored", F.convert({"type": "spot_price", "symbol": ".DEXETHUSD", "price": 3000}, tok_of, 1) == [])
    l1 = F.convert({"type": "l1_orderbook", "symbol": ce.symbol, "best_bid": "534.0", "best_ask": "553.0",
                    "bid_qty": "20419", "ask_qty": "9628", "timestamp": 1794670792429927}, tok_of, 1)[0]
    check("l1 bid/ask", l1[0] == ce.token and l1[2] == 534.0 and l1[3] == 553.0, l1)
    check("l1 sizes", l1[4] == 20419 and l1[5] == 9628, l1)
    check("l1 keeps ltp", l1[1] is None and l1[7] is None)
    check("l1 time seconds", l1[10] == 1794670792, l1[10])
    tk = F.convert({"type": "v2/ticker", "symbol": ce.symbol, "close": 900.0, "open": 257.0, "size": 137423,
                    "oi_contracts": "7906", "timestamp": 1794670793021504,
                    "quotes": {"best_bid": "890", "best_ask": "910", "bid_size": "5", "ask_size": "7"}}, tok_of, 1)[0]
    check("ticker ltp", tk[1] == 900.0)
    check("ticker oi contracts", tk[7] == 7906)
    check("ticker volume", tk[6] == 137423)
    check("ticker prev = 24h open, change", tk[8] == 257.0 and tk[11] == 643.0, tk)
    l2 = F.convert({"type": "l2_orderbook", "symbol": ce.symbol,
                    "buy": [{"limit_price": "790.0", "size": 14444}, {"limit_price": "788.0", "size": 14482}],
                    "sell": [{"limit_price": "800.0", "size": 10}]}, tok_of, 1)[0]
    check("depth levels", l2[12] == ([(790.0, 14444), (788.0, 14482)], [(800.0, 10)]), l2[12])
    check("depth best", l2[2] == 790.0 and l2[3] == 800.0)
    check("unknown symbol ignored", F.convert({"type": "l1_orderbook", "symbol": "X"}, tok_of, 1) == [])
    check("other message ignored", F.convert({"type": "subscriptions"}, tok_of, 1) == [])


def test_feed_payload():
    u = universe()
    f = F.DeltaFeed(u, lambda t: None, spot_token=C.BTC_SPOT_TOKEN)
    toks = [c.token for c in list(u.by_token.values()) if c.kind == "CE"][:2]
    f.want(toks + [C.BTC_SPOT_TOKEN, 999])          # the index and unknown ids are not "contracts"
    check("wanted contracts only", f.wanted == set(toks), f.wanted)
    pl = f._payload(f.wanted, False, spot=True)["payload"]["channels"]
    names = [c["name"] for c in pl]
    check("channels", names == ["spot_price", "l1_orderbook", "v2/ticker"], names)
    f.want_depth(toks[:1])
    check("depth wanted", f.depth_wanted == {toks[0]})
    check("status shape", {"connected", "wanted", "ticks", "last_tick_at"} <= set(f.status()))


def test_paper_usd():
    u = universe()
    ch = u.products["BTC"].chains["2026-11-15"]
    ce = ch[min(ch, key=lambda s: abs(s - 65000))]["CE"]
    q = {ce.token: {"ltp": 550.0, "bid": 540.0, "ask": 560.0, "bq": 100, "aq": 100, "depth": None}}
    path = TMP / "paper_book_test.json"
    path.unlink(missing_ok=True)
    b = P.PaperBook(u, lambda t: q.get(t, {}), path=path)
    r = b.place(ce.token, "S", 100, kind="MKT")
    check("sell 100 contracts", r.get("ok"), r)
    pos = {p["token"]: p for p in b.positions()}[ce.token]
    check("short 100 @ bid", pos["net"] == -100 and pos["avg"] == 540.0, pos)
    q[ce.token] = {"ltp": 500.0, "bid": 495.0, "ask": 505.0, "bq": 100, "aq": 100, "depth": None}
    r = b.place(ce.token, "B", 100, kind="MKT")
    pos = {p["token"]: p for p in b.positions()}[ce.token]
    # (540 - 505) points x 100 contracts x 0.001 = $3.50
    check("realized USD", abs(pos["realized"] - 3.5) < 1e-6, pos["realized"])
    check("over the cap refused", not b.place(ce.token, "S", C.MAX_LOTS_PER_ORDER + 1, kind="MKT").get("ok"))


def test_merge_new_strikes():
    """15-Nov: Delta lists new strikes during the day; the desk's day list missed them (16-Nov puts below
    63600 'not shown'). merge() adds them, both legs, in strike order, and the feed learns their symbols."""
    rows = json.loads((HERE / "testdata" / "delta_products_sample.json").read_text(encoding="utf-8"))
    day = [r for r in rows if r.get("settlement_time", "").startswith("2026-11-16")]
    k_new = sorted({float(r["strike_price"]) for r in day})[0]           # pretend the lowest strike came later
    early = [r for r in rows if not (r.get("settlement_time", "").startswith("2026-11-16")
                                     and float(r["strike_price"]) == k_new)]
    u = D.parse(early, now=NOW)
    check("strike missing at first", k_new not in u.products["BTC"].chains["2026-11-16"])
    f = F.DeltaFeed(u, lambda t: None, spot_token=C.BTC_SPOT_TOKEN)
    new = D.merge(u, rows, now=NOW)
    check("both legs added", sorted(c.kind for c in new) == ["CE", "PE"], [c.symbol for c in new])
    ch = u.products["BTC"].chains["2026-11-16"]
    check("strike now in chain", k_new in ch and set(ch[k_new]) == {"CE", "PE"})
    check("chain kept in strike order", list(ch) == sorted(ch))
    check("tokens known", all(c.token in u.by_token for c in new))
    f.add_contracts(new)
    check("feed knows the new symbols", all(f.tok_of.get(c.symbol) == c.token for c in new))
    f.want([c.token for c in new])
    check("new contracts can be subscribed", f.wanted == {c.token for c in new})
    check("nothing new the second time", D.merge(u, rows, now=NOW) == [])


def test_imbalance_delta_names():
    """14-Nov: the CE/PE imbalance read calls/puts from names ending in CE/PE -> Delta's C-BTC-… / P-BTC-…
    shorts counted as nothing (strip 0, chart empty)."""
    from live_trading_options.mcx_terminal import mtm as M
    check("kind from Delta call name", M.opt_kind({"symbol": "C-BTC-65800-151126"}) == "CE")
    check("kind from Delta put name", M.opt_kind({"symbol": "P-BTC-65800-151126"}) == "PE")
    check("kind from Kotak name", M.opt_kind({"symbol": "CRUDEOIL17NOV269300PE"}) == "PE")
    check("own kind wins", M.opt_kind({"symbol": "X", "kind": "CE"}) == "CE")
    pos = [{"token": 1, "symbol": "C-BTC-65800-151126", "name": "BTC", "net": -1000, "avg": 500.0, "realized": 0.0, "mult": 0.001},
           {"token": 2, "symbol": "P-BTC-65800-151126", "name": "BTC", "net": -1000, "avg": 500.0, "realized": 0.0, "mult": 0.001}]
    px = {1: 600.0, 2: 400.0}
    h = M.MtmHistory(path=TMP / "mtm_imb_test.json")
    row = h._row(pos, px.get, 1.0)
    flat = json.dumps(row)
    # short CE 600 x 1000 x 0.001 = 600, short PE 400 -> (600 - 400) / 1000 = +20 %
    check("imbalance +20 % on Delta shorts", "20.0" in flat, flat)


def test_no_live():
    u = universe()
    lb = NoLiveBook(u, lambda t: {})
    c = next(x for x in u.by_token.values() if x.kind == "CE")
    r = lb.place(c.token, "S", 1)
    check("live refused", r["ok"] is False and "PAPER only" in r["msg"], r)
    check("nothing open", lb.open_positions() == 0 and lb.working() == 0 and lb.tokens() == set())
    check("broker ok (no warnings)", lb.broker["ok"] and lb.positions_fresh())
    check("no broker rows", lb._rows() == [])
    check("live book file named by desk day", lb.path.name == f"live_book_{C.day()}.json", lb.path.name)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as e:
                FAIL += 1
                import traceback
                traceback.print_exc()
                print(f"FAIL {name} crashed: {e}")
    print(f"{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
