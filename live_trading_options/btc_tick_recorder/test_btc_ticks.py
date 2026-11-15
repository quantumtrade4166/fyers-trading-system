"""
btc_tick_recorder/test_btc_ticks.py — offline tests (no network): message decoding, full-state rows, store round trip.

    .venv\\Scripts\\python.exe -m pytest live_trading_options/btc_tick_recorder/test_btc_ticks.py -q
"""
import json
import time

from live_trading_options.btc_tick_recorder import service as S
from live_trading_options.tick_recorder import store as ST

L1 = {"type": "l1_orderbook", "symbol": "C-BTC-67400-141126", "best_bid": "239.0", "best_ask": "242.0",
      "bid_qty": "8517", "ask_qty": "6129", "last_updated_at": 1794636147953643}
MARK = {"type": "mark_price", "symbol": "MARK:C-BTC-67400-141126", "price": "239.55661016", "timestamp": 1794636146576147}
TICKER = {"type": "v2/ticker", "symbol": "C-BTC-67400-141126", "oi_contracts": "1791", "size": 99616}
TRADE = {"type": "all_trades", "symbol": "P-BTC-65200-151126", "price": "149.0", "size": 25, "buyer_role": "taker",
         "seller_role": "maker", "timestamp": 1794665929999820}


def test_quote_rows_carry_full_state():
    st = S.State()
    idx, t = st.on(L1, 100.5)
    assert idx == "BTC" and t.key == "delta|C-BTC-67400-141126"
    assert (t.ltp, t.bid, t.ask, t.bid_qty, t.ask_qty, t.exch_ts) == (0.0, 239.0, 242.0, 8517, 6129, 1794636147)
    assert st.on(TICKER, 101.0) is None                       # oi / volume carried, no row of its own
    idx, t = st.on(MARK, 101.5)
    assert (t.ltp, t.bid, t.ask, t.volume, t.oi) == (239.55661016, 239.0, 242.0, 99616, 1791)


def test_empty_side_has_no_size():
    st = S.State()
    _, t = st.on({**L1, "best_bid": "0", "bid_qty": "0"}, 1.0)
    assert t.bid is None and t.bid_qty is None and t.ask == 242.0


def test_trade_row_side_and_size():
    _, t = S.State().on(TRADE, 5.0)
    assert t.key == "delta|P-BTC-65200-151126" and t.ltp == 149.0
    assert (t.bid_qty, t.ask_qty, t.volume, t.exch_ts) == (25, None, 25, 1794665929)
    _, t2 = S.State().on({**TRADE, "buyer_role": "maker", "seller_role": "taker"}, 5.0)
    assert (t2.bid_qty, t2.ask_qty) == (None, 25)


def test_spot_and_ignored_messages():
    st = S.State()
    idx, t = st.on({"type": "spot_price", "symbol": ".DEXBTUSD", "price": 67312.6}, 9.25)
    assert idx == "BTC" and t.key == "delta|.DEXBTUSD" and t.ltp == 67312.6
    assert st.on({"type": "all_trades_snapshot", "symbol": "X"}, 1.0) is None
    assert st.on({"type": "subscriptions"}, 1.0) is None


def test_payload_subscribes_every_channel():
    rec = S.Recorder.__new__(S.Recorder)
    j = json.loads(S.Recorder.payload(rec, ["C-BTC-1-1", "P-BTC-1-1"]))
    names = {c["name"]: c["symbols"] for c in j["payload"]["channels"]}
    assert set(names) == {"spot_price", "l1_orderbook", "mark_price", "v2/ticker", "all_trades"}
    assert names["mark_price"] == ["MARK:C-BTC-1-1", "MARK:P-BTC-1-1"]


def test_store_round_trip_and_gap_log(tmp_path):
    rec = S.Recorder(tmp_path, start_store=False)
    rec.store.packer = ST.pack
    rec.products.update({"C-BTC-67400-141126": {"kind": "CE", "expiry": "2026-11-14", "strike": 67400.0},
                         "P-BTC-65200-151126": {"kind": "PE", "expiry": "2026-11-15", "strike": 65200.0}})
    t0 = time.time() - 3600                                   # an old slot, so it packs at once
    for i, m in enumerate((L1, MARK, TRADE)):
        out = rec.state.on(m, t0 + i * 0.001)
        rec.store.add(*out)
    rec.store.flush()
    rec.store.compact_due(all_=True)
    day = next(tmp_path.iterdir()).name
    c, rows = ST.read_day(day, "BTC", tmp_path)
    assert [r[1] for r in rows] == ["delta|C-BTC-67400-141126"] * 2
    assert rows[1][2] == 239.56 and rows[1][3] == 239.0 and rows[1][4] == 242.0     # cents
    assert c["delta|C-BTC-67400-141126"]["strike"] == 67400.0
    _c, tr = ST.read_day(day, "BTCTRD", tmp_path)
    assert len(tr) == 1 and tr[0][2] == 149.0 and tr[0][5] == 25
    rec.down_since, rec.down_reason = time.time() - 5, "test"
    rec.gap_closed(time.time())
    g = list(tmp_path.rglob("BTC_gaps.jsonl"))
    assert g and json.loads(g[0].read_text().splitlines()[-1])["reason"] == "test" and rec.down_since is None
