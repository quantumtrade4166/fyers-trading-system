"""
tests/test_spread.py — the maths the whole monitor rests on.

Run:  .venv\\Scripts\\python.exe crypto\\btc_arbitrage\\tests\\test_spread.py
(also collected by pytest). No network.
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fees import FeeModel
from core.spread_engine import (compute, quality, threshold_usd, size_ok,
                                BUY_DELTA, BUY_BINANCE)
from core.opportunity_detector import OpportunityDetector
from core.depth import walk
from core.market_data import MarketData
from exchanges.delta import parse_l1, parse_funding
from exchanges.binance import parse_book_ticker, to_btc

PASS = FAIL = 0
NOFEE = FeeModel(0.0, 0.0, 0.0, "entry")
FEE = FeeModel(0.0005, 0.18, 0.0005, "round_trip")


def check(name, got, want, tol=1e-9):
    global PASS, FAIL
    ok = abs(got - want) <= tol if isinstance(want, float) and got is not None else got == want
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        if "pytest" in sys.modules:
            raise AssertionError(f"{name}: got {got!r}, want {want!r}")


def test_case_a_buy_delta_sell_binance():
    # Delta ask 100,000, Binance bid 100,100 -> +100 gross, BUY DELTA / SELL BINANCE
    s = compute(99_990, 100_000, 100_100, 100_110, NOFEE)
    check("A: s1 = Binance bid - Delta ask = +100", s.s1, 100.0)
    check("A: direction BUY DELTA", s.direction, BUY_DELTA)
    check("A: gross +100", s.gross, 100.0)
    check("A: signed edge is POSITIVE", s.edge, 100.0)
    check("A: s1_pct = 100/100000 = 0.1%", s.s1_pct, 0.1)
    check("A: opposite direction is negative", s.s2 < 0, True)


def test_case_b_buy_binance_sell_delta():
    # Binance ask 100,000, Delta bid 100,100 -> +100 gross, BUY BINANCE / SELL DELTA
    s = compute(100_100, 100_110, 99_990, 100_000, NOFEE)
    check("B: s2 = Delta bid - Binance ask = +100", s.s2, 100.0)
    check("B: direction BUY BINANCE", s.direction, BUY_BINANCE)
    check("B: gross +100", s.gross, 100.0)
    check("B: signed edge is NEGATIVE (other side of zero)", s.edge, -100.0)
    check("B: s2_pct = 100/100000 = 0.1%", s.s2_pct, 0.1)


def test_case_c_normal_markets_have_no_edge():
    # Delta ask > Binance bid AND Binance ask > Delta bid -> nothing executable
    for d_bid, d_ask, b_bid, b_ask, tag in [
        (100_000, 100_010, 100_000, 100_010, "identical books"),
        (100_000, 100_010, 100_005, 100_015, "Binance mid +5, still overlapping"),
        (100_003, 100_004, 100_000, 100_008, "Delta inside Binance"),
    ]:
        s = compute(d_bid, d_ask, b_bid, b_ask, NOFEE)
        check(f"C [{tag}]: s1 <= 0", s.s1 <= 0, True)
        check(f"C [{tag}]: s2 <= 0", s.s2 <= 0, True)
        check(f"C [{tag}]: edge = 0", s.edge, 0.0)
        check(f"C [{tag}]: best gross not positive", s.gross <= 0, True)


def test_original_prompt_sign_bug_is_not_reproduced():
    # The prompt's rule `net = s1 if s1>=0 else -s2` gives +10 here — a fake
    # positive in a perfectly normal market. Our signed edge must be 0.
    s = compute(100_000, 100_010, 100_000, 100_010, NOFEE)
    prompt_rule = s.s1 if s.s1 >= 0 else -s.s2
    check("prompt rule would show +10 (the bug)", prompt_rule, 10.0)
    check("our edge shows 0", s.edge, 0.0)
    check("mid spread is 0 (the chart line)", s.mid_spread, 0.0)


def test_mid_spread_and_ribbon():
    s = compute(100_000, 100_010, 100_050, 100_060, NOFEE)
    check("mid spread = 100055 - 100005 = +50", s.mid_spread, 50.0)
    check("mid_spread_pct = 50/100005*100", s.mid_spread_pct, 50 / 100_005 * 100)
    check("ribbon low  s1  = mid - both half-spreads = 40", s.s1, 40.0)
    check("ribbon high -s2 = mid + both half-spreads = 60", -s.s2, 60.0)


def test_at_most_one_direction_positive():
    import random
    rnd = random.Random(7)
    bad = 0
    for _ in range(20_000):
        d_bid = rnd.uniform(99_000, 101_000); d_ask = d_bid + rnd.uniform(0, 20)
        b_bid = rnd.uniform(99_000, 101_000); b_ask = b_bid + rnd.uniform(0, 20)
        s = compute(d_bid, d_ask, b_bid, b_ask, NOFEE)
        bad += (s.s1 > 0 and s.s2 > 0)
    check("never both directions positive (20k random books)", bad, 0)


def test_fees():
    # Delta 0.05% x 1.18 = 0.059%, Binance 0.05% -> 0.109% per side, 0.218% round trip
    check("round-trip cost pct = 0.218%", round(FEE.cost_pct(), 6), 0.218)
    check("round trip on $100k both legs = $218", round(FEE.cost(100_000, 100_000), 6), 218.0)
    entry = FeeModel(0.0005, 0.18, 0.0005, "entry")
    check("entry-only on $100k = $109", round(entry.cost(100_000, 100_000), 6), 109.0)
    s = compute(99_990, 100_000, 100_100, 100_110, entry)
    check("A after entry fees: 100 - (100000*0.00059 + 100100*0.0005)", round(s.net1, 6),
          round(100 - (100_000 * 0.00059 + 100_100 * 0.0005), 6))
    check("A after entry fees is NOT profitable (fees 109 > gross 100)", s.net1 < 0, True)
    check("edge_net is 0 when fees eat the gross", s.edge_net, 0.0)
    try:
        FeeModel(0.0005, 0.18, 0.0005, "bogus")
        check("bad FEE_MODE rejected", False, True)
    except ValueError:
        check("bad FEE_MODE rejected", True, True)


def test_threshold_and_quality():
    check("threshold = max($5, 0.005% of 84000=$4.2) = $5", threshold_usd(84_000, 5, 0.005), 5.0)
    check("threshold = max($1, 0.01% of 84000=$8.4)", round(threshold_usd(84_000, 1, 0.01), 6), 8.4)
    check("fresh + connected -> OK", quality(100, 200, True, True, 3000, 1500), "OK")
    check("Delta stale -> STALE", quality(3500, 200, True, True, 3000, 1500), "STALE")
    check("quotes 2 s apart -> DESYNC", quality(2100, 50, True, True, 3000, 1500), "DESYNC")
    check("socket down -> DISCONNECTED", quality(100, 100, False, True, 3000, 1500), "DISCONNECTED")
    check("no quote yet -> NO_DATA", quality(None, 100, True, True, 3000, 1500), "NO_DATA")


def test_detector():
    det = OpportunityDetector(min_ms=500, change_usd=10)
    good = compute(99_990, 100_000, 100_300, 100_310, NOFEE)    # gross +300
    flat = compute(100_000, 100_010, 100_000, 100_010, NOFEE)
    check("t=0 blip: no event yet", det.update(good, "OK", 5, 0), [])
    check("t=300: still pending", det.update(good, "OK", 5, 300), [])
    ev = det.update(good, "OK", 5, 600)
    check("t=600: STARTED once persisted 500ms", [e["event"] for e in ev], ["STARTED"])
    check("no repeat while unchanged", det.update(good, "OK", 5, 700), [])
    ev = det.update(good, "STALE", 5, 800)
    check("stale data ENDS the opportunity", [e["event"] for e in ev], ["ENDED"])
    check("end reason names the data problem", ev[0]["reason"], "data STALE")
    det.update(good, "OK", 5, 1000)
    check("short blip after restart then gone: no event", det.update(flat, "OK", 5, 1200), [])
    det.update(good, "OK", 5, 2000); det.update(good, "OK", 5, 2600)
    bigger = compute(99_990, 100_000, 100_320, 100_330, NOFEE)   # +320, moved 20
    check("material change -> CHANGED", [e["event"] for e in det.update(bigger, "OK", 5, 2700)], ["CHANGED"])
    check("drop below threshold -> ENDED", [e["event"] for e in det.update(flat, "OK", 5, 2800)], ["ENDED"])


def test_depth_and_size():
    asks = [(100_000, 0.003), (100_005, 0.010)]
    check("walk 0.01 BTC: 0.003@100000 + 0.007@100005 = 100003.5", walk(asks, 0.01), 100_003.5)
    check("walk more than the book -> None", walk(asks, 1.0), None)
    check("size_ok BUY_DELTA needs Delta ask + Binance bid size",
          size_ok(BUY_DELTA, 0.0, 0.02, 0.02, 0.0, 0.01), True)
    check("size_ok fails when Delta ask too thin",
          size_ok(BUY_DELTA, 1, 0.005, 1, 1, 0.01), False)


def test_parsers_on_real_messages():
    # captured live 2026-10-03
    d = {"ask_qty": "3562", "best_ask": "84013.0", "best_bid": "84012.5", "bid_qty": "2621",
         "last_sequence_no": 10072595, "last_updated_at": 1790968808376292, "product_id": 27,
         "symbol": "BTCUSD", "timestamp": 1790968808420232, "type": "l1_orderbook"}
    bid, bq, ask, aq, ts = parse_l1(d, "BTCUSD", 0.001)
    check("delta bid", bid, 84012.5)
    check("delta bid qty 2621 contracts = 2.621 BTC", bq, 2.621)
    check("delta ask qty 3562 contracts = 3.562 BTC", aq, 3.562)
    check("delta ts us -> ms", ts, 1790968808376.292)
    check("delta: other symbol ignored", parse_l1(d, "ETHUSD", 0.001), None)
    f = {"type": "funding_rate", "symbol": "BTCUSD", "funding_rate": 0.003923285466234529,
         "funding_interval": 28800, "next_funding_realization": 1790985600000000}
    r, iv, nxt = parse_funding(f, "BTCUSD")
    check("delta funding percent -> fraction", r, 0.003923285466234529 / 100)
    check("delta funding interval 8h", iv, 8.0)
    b = {"e": "bookTicker", "u": 11721678133241, "s": "BTCUSDT", "ps": "BTCUSDT", "b": "84012.90",
         "B": "5.343", "a": "84013.00", "A": "7.188", "T": 1790968808182, "E": 1790968808182}
    bid, bq, ask, aq, ts = parse_book_ticker(b, "BTCUSDT", "btc")
    check("binance usdm bid", bid, 84012.9)
    check("binance usdm qty already BTC", bq, 5.343)
    check("binance ts ms", ts, 1790968808182.0)
    c = {"e": "bookTicker", "s": "BTCUSD_PERP", "b": "83993.8", "B": "140", "a": "83993.9",
         "A": "9978", "T": 1790968808075}
    bid, bq, ask, aq, ts = parse_book_ticker(c, "BTCUSD_PERP", "usd_100")
    check("binance coinm 140 x $100 / 83993.8 = BTC", bq, 140 * 100 / 83993.8)
    check("to_btc unknown unit raises", _raises(lambda: to_btc(1, 1, "lots")), True)


def test_market_data_rejects_bad_quotes():
    md = MarketData()
    check("good quote accepted", md.put("delta", 100, 1, 101, 1, 0), True)
    check("missing ask rejected", md.put("delta", 100, 1, None, 1, 0), False)
    check("crossed quote rejected", md.put("delta", 102, 1, 101, 1, 0), False)
    check("previous valid quote kept", md.delta.quote.ask, 101)
    check("rejections counted", md.delta.rejected, 2)


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
