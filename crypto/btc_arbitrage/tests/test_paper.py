"""
tests/test_paper.py — the zero-fee paper trader and the Delta order book.

Run:  .venv\\Scripts\\python.exe crypto\\btc_arbitrage\\tests\\test_paper.py
(also collected by pytest). No network.
"""
import sys
import json
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.fees import FeeModel
from core.market_data import Depth
from core.orderbook import DeltaBook, ResyncRequired
from core.paper import PaperTrader, depth_spreads, clean_control
from core.spread_engine import BUY_DELTA, BUY_BINANCE
from exchanges.binance import parse_depth

PASS = FAIL = 0
FEE = FeeModel(0.0005, 0.18, 0.0005, "round_trip")


def check(name, got, want, tol=1e-6):
    global PASS, FAIL
    ok = (abs(got - want) <= tol) if isinstance(want, float) and isinstance(got, (int, float)) else got == want
    if ok:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")
        if "pytest" in sys.modules:
            raise AssertionError(f"{name}: got {got!r}, want {want!r}")


def book(bid, ask, qty=5.0):
    """A simple two-level book: qty at the touch, plenty one tick away."""
    return Depth([(bid, qty), (bid - 1, 50)], [(ask, qty), (ask + 1, 50)], 0, 0)


def trader(tmp, **ctl):
    d = Path(tmp)
    c = {"enabled": True, "entry_usd": 20, "exit_band_usd": 1, "latency_ms": 300, **ctl}
    (d / "ctl.json").write_text(json.dumps(c))
    events = []
    t = PaperTrader(1.0, FEE, d / "ctl.json", d / "state.json", d / "trades.jsonl",
                    on_event=events.append)
    return t, events


def test_depth_walk_for_one_btc():
    dd = Depth([(100_000, 0.3), (99_999, 0.7)], [(100_001, 0.4), (100_003, 0.6)], 0, 0)
    bd = book(100_050, 100_051)
    sp = depth_spreads(dd, bd, 1.0)
    check("Delta 1-BTC ask = 0.4@100001 + 0.6@100003 = 100002.2", sp["d_ask"], 100_002.2)
    check("Delta 1-BTC bid = 0.3@100000 + 0.7@99999 = 99999.3", sp["d_bid"], 99_999.3)
    check("s1 for 1 BTC = 100050 - 100002.2 = 47.8", sp["s1"], 47.8)
    thin = Depth([(100_000, 0.2)], [(100_001, 0.2)], 0, 0)
    check("book too thin for 1 BTC -> None", depth_spreads(thin, bd, 1.0), None)
    check("missing book -> None", depth_spreads(None, bd, 1.0), None)


def test_round_trip_buy_delta():
    with tempfile.TemporaryDirectory() as tmp:
        t, ev = trader(tmp)
        # Binance rich by ~$30 for 1 BTC: buy Delta @100000, sell Binance @100030
        sp = depth_spreads(book(99_999, 100_000), book(100_030, 100_031), 1.0)
        t.step(sp, True, now_mono=0.0)
        check("signal -> ENTERING, not filled yet (latency)", t.state, "ENTERING")
        t.step(sp, True, now_mono=0.1)
        check("still ENTERING at 100 ms", t.state, "ENTERING")
        # 300 ms later the gap has shrunk to $25 — the fill gets the LATER books
        sp2 = depth_spreads(book(100_004, 100_005), book(100_030, 100_031), 1.0)
        t.step(sp2, True, now_mono=0.31)
        check("filled -> OPEN", t.state, "OPEN")
        check("direction buy Delta", t.position["direction"], BUY_DELTA)
        check("entry spread is the FILL (25), not the signal (30)", t.position["entry_spread"], 25.0)
        check("slippage vs signal = -5", t.position["slippage_vs_signal"], -5.0)
        # books converge: Delta 100030/100031, Binance 100030/100031 -> unwind s2 = 100030-100031 = -1
        conv = depth_spreads(book(100_030, 100_031), book(100_030, 100_031), 1.0)
        t.step(conv, True, now_mono=5.0)
        check("unwind -1 >= -exit_band(1) -> EXITING", t.state, "EXITING")
        t.step(conv, True, now_mono=5.31)
        check("exit filled -> FLAT", t.state, "FLAT")
        tr = json.loads((Path(tmp) / "trades.jsonl").read_text().splitlines()[0])
        # P&L = (D_bid2 - D_ask1) + (B_bid1 - B_ask2) = (100030-100005) + (100030-100031) = 24
        check("P&L = entry 25 + exit -1 = $24 on 1 BTC", tr["pnl"], 24.0)
        check("P&L equals the two legs' real P&L",
              tr["pnl"], (tr["exit_delta_px"] - tr["entry_delta_px"]) + (tr["entry_binance_px"] - tr["exit_binance_px"]))
        fees = (100_005 * 0.00059 + 100_030 * 0.0005) + (100_030 * 0.00059 + 100_031 * 0.0005)
        check("fees_if_charged = 4 fills", tr["fees_if_charged"], round(fees, 2), tol=0.01)
        check("pnl_with_fees = pnl - fees", tr["pnl_with_fees"], round(24 - fees, 2), tol=0.01)
        check("events: ENTRY then EXIT", [e["event"] for e in ev], ["PAPER_ENTRY", "PAPER_EXIT"])
        check("realized total", t.realized, 24.0)


def test_round_trip_buy_binance():
    with tempfile.TemporaryDirectory() as tmp:
        t, _ = trader(tmp, latency_ms=0)
        # Delta rich: sell Delta @100040 (its bid), buy Binance @100010 (its ask) -> s2 = 30
        sp = depth_spreads(book(100_040, 100_041), book(100_009, 100_010), 1.0)
        t.step(sp, True, now_mono=0.0)
        check("latency 0 -> filled on the same step", t.state, "OPEN")
        check("direction buy Binance", t.position["direction"], BUY_BINANCE)
        check("entry spread 30", t.position["entry_spread"], 30.0)
        conv = depth_spreads(book(100_020, 100_021), book(100_020, 100_021), 1.0)
        t.step(conv, True, now_mono=1.0)
        check("converged -> closed", t.state, "FLAT")
        tr = json.loads((Path(tmp) / "trades.jsonl").read_text().splitlines()[0])
        # close: sell Binance @100020, buy Delta @100021 -> (100020-100010) + (100040-100021) = 29
        check("P&L = 30 + (-1) = 29", tr["pnl"], 29.0)


def test_no_entry_below_threshold_or_bad_data():
    with tempfile.TemporaryDirectory() as tmp:
        t, _ = trader(tmp)
        small = depth_spreads(book(99_999, 100_000), book(100_015, 100_016), 1.0)   # s1 = 15
        t.step(small, True, 0.0)
        check("$15 < $20 entry -> stays FLAT", t.state, "FLAT")
        big = depth_spreads(book(99_999, 100_000), book(100_050, 100_051), 1.0)
        t.step(big, False, 0.0)
        check("data not clean -> no entry", t.state, "FLAT")
        t.step(big, True, 0.0)
        t.step(None, False, 0.5)
        check("depth gone at fill time -> entry skipped", t.state, "FLAT")


def test_disabled_and_toggle_off_closes():
    with tempfile.TemporaryDirectory() as tmp:
        t, ev = trader(tmp, enabled=False, latency_ms=0)
        big = depth_spreads(book(99_999, 100_000), book(100_050, 100_051), 1.0)
        t.step(big, True, 0.0)
        check("toggle OFF -> never enters", t.state, "FLAT")
        t.control["enabled"] = True
        t.step(big, True, 0.1)
        check("toggle ON -> enters", t.state, "OPEN")
        t.control["enabled"] = False
        t.step(big, True, 0.2)
        check("toggle OFF with a position -> closed at market", t.state, "FLAT")
        check("exit reason recorded", ev[-1]["reason"], "toggle OFF")


def test_open_position_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        t, _ = trader(tmp, latency_ms=0)
        t.step(depth_spreads(book(99_999, 100_000), book(100_050, 100_051), 1.0), True, 0.0)
        check("open before restart", t.state, "OPEN")
        t2, _ = trader(tmp, latency_ms=0)
        check("restart resumes OPEN", t2.state, "OPEN")
        check("same entry spread", t2.position["entry_spread"], t.position["entry_spread"])


def test_control_validation():
    c = clean_control({"enabled": 1, "entry_usd": "-5", "exit_band_usd": 2, "latency_ms": 99999})
    check("negative entry rejected -> default 20", c["entry_usd"], 20.0)
    check("latency beyond 5 s rejected -> default 300", c["latency_ms"], 300)
    check("valid exit band kept", c["exit_band_usd"], 2.0)
    check("garbage -> defaults, OFF", clean_control("x")["enabled"], False)


def test_delta_book():
    b = DeltaBook(0.001)
    b.apply({"action": "snapshot", "bids": [["100", "1000"], ["99", "2000"]],
             "asks": [["101", "500"], ["102", "3000"]], "sequence_no": 10, "timestamp": 5_000_000})
    check("snapshot best bid", b.top_bids[0], (100.0, 1.0))
    check("contracts -> BTC", b.top_asks[1], (102.0, 3.0))
    b.apply({"action": "update", "bids": [["100", "0"]], "asks": [["100.5", "100"]], "sequence_no": 11})
    check("size 0 removes a level", b.top_bids[0][0], 99.0)
    check("new level inserted in order", b.top_asks[0], (100.5, 0.1))
    try:
        b.apply({"action": "update", "bids": [], "asks": [], "sequence_no": 13})
        check("sequence gap raises", False, True)
    except ResyncRequired:
        check("sequence gap raises", True, True)
    check("gap resets the book", b.top_bids, [])
    b2 = DeltaBook(0.001)
    check("update before snapshot ignored", b2.apply({"action": "update", "bids": [["1", "1"]], "sequence_no": 1}), False)


def test_binance_depth_parse():
    m = {"e": "depthUpdate", "s": "BTCUSDT", "T": 1791045556069,
         "b": [["84797.40", "17.157"], ["84797.30", "0.007"]],
         "a": [["84797.60", "0.116"], ["84797.50", "0.412"]]}
    bids, asks, ts = parse_depth(m, "BTCUSDT", "btc")
    check("binance bids best first", bids[0], (84797.4, 17.157))
    check("binance asks sorted best first", asks[0], (84797.5, 0.412))
    check("binance depth ts", ts, 1791045556069.0)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
