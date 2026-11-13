"""cepe_imbalance/test_quotes.py — every tick is recorded and reads back with contract details."""
import time
from types import SimpleNamespace as NS

from live_trading_options.cepe_imbalance import quotes


def _q(ltp, bid, ask):
    return NS(ltp=ltp, bid=bid, ask=ask, bid_qty=5, ask_qty=7, volume=100, oi=9)


def test_record_and_read_back(tmp_path):
    u = NS(by_key={"mcx_fo|1": NS(symbol="GOLD26NOV139000CE", kind="CE", expiry="2026-11-25", strike=139000.0)})
    r = quotes.QuoteRecorder(u, folder=tmp_path, start=False)
    t = 1794454500.0                                     # 2026-11-12 IST
    r.add("GOLD", "mcx_fo|1", _q(2700, 2690, 2710), t)
    r.add("GOLD", "mcx_fo|2", _q(139100, None, None), t + 0.4)   # unknown key (spot/future style)
    r.flush()
    r.add("GOLD", "mcx_fo|1", _q(2705, 2695, 2712), t + 1)
    r.flush()
    meta, rows = quotes.read("2026-11-12", "GOLD", folder=tmp_path)
    assert meta["mcx_fo|1"] == ["GOLD26NOV139000CE", "CE", "2026-11-25", 139000.0]
    assert meta["mcx_fo|2"][1] == "SPOT"
    assert [x[2] for x in rows] == [2700, 139100, 2705] and rows[0][3:5] == [2690, 2710]
    assert r.stats["frames"] == 2 and r.stats["rows"] == 3


def test_torn_last_frame_is_skipped(tmp_path):
    u = NS(by_key={})
    r = quotes.QuoteRecorder(u, folder=tmp_path, start=False)
    r.add("NIFTY", "k", _q(1, 1, 1), 1794454500.0)
    r.flush()
    p = tmp_path / "2026-11-12" / "NIFTY.qrec"
    p.write_bytes(p.read_bytes() + b"\x50\x00\x00\x00abc")    # engine killed mid-write
    assert len(quotes.read("2026-11-12", "NIFTY", folder=tmp_path)[1]) == 1
