"""tick_recorder tests: exact round trip, 15-min slots + compaction, restart, torn frame, contract selection."""
import datetime as dt
from types import SimpleNamespace as NS

from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.tick_recorder import service as S
from live_trading_options.tick_recorder import store

T = 1794545400.0                                       # 2026-11-13 10:20:00 IST (slot 1015)


def info(key):
    return ("NIFTY26N1725000CE", "CE", "2026-11-17", 25000.0) if key == "nse_fo|1" else ("Nifty 50", "INDEX", "", 0)


def tk(key, t, ltp, bid=None, ask=None, bq=None, oi=None, vol=1234):
    return Tick(key, ltp, bid, ask, bq, bq, vol, oi, int(t), t)


def _files(tmp_path):
    return sorted(p.name for p in (tmp_path / "2026-11-13").iterdir())


def test_round_trip_exact_through_compaction(tmp_path):
    s = store.TickStore(tmp_path, info, start=False, packer=store.pack)
    s.add("NIFTY", tk("nse_fo|1", T, 101.05, 101.0, 101.1, 75, 1_500_000, vol=3_900_000_000))
    s.add("NIFTY", tk("nse_cm|Nifty 50", T + 0.123, 25012.35, vol=None))
    s.flush()
    s.add("NIFTY", tk("nse_fo|1", T + 1.5, 101.15, 101.1, 101.2, 150, 1_500_075))
    s.flush()
    before = store.read_day("2026-11-13", "NIFTY", tmp_path)
    assert _files(tmp_path) == ["NIFTY_1015.trec.tmp"]
    s.compact_due(now=T + 3600)
    assert _files(tmp_path) == ["NIFTY_1015.tcol"]                       # journal gone, compact file kept
    c, rows = store.read_day("2026-11-13", "NIFTY", tmp_path)
    assert (c, rows) == before
    assert c["nse_fo|1"] == {"symbol": "NIFTY26N1725000CE", "kind": "CE", "expiry": "2026-11-17", "strike": 25000.0}
    assert [r[1] for r in rows] == ["nse_fo|1", "nse_cm|Nifty 50", "nse_fo|1"]
    assert rows[0][2:5] == (101.05, 101.0, 101.1) and rows[0][7] == 3_900_000_000 and rows[0][8] == 1_500_000
    assert rows[1][2] == 25012.35 and rows[1][3] is None and rows[1][7] is None
    assert abs(rows[1][0] - (T + 0.123)) < 0.002 and rows[2][2] == 101.15


def test_slot_not_compacted_until_over(tmp_path):
    s = store.TickStore(tmp_path, info, start=False, packer=store.pack)
    s.add("NIFTY", tk("nse_fo|1", T, 100.0))
    s.add("NIFTY", tk("nse_fo|1", T + 900, 90.0))                         # 10:35 -> slot 1030
    s.flush()
    assert _files(tmp_path) == ["NIFTY_1015.trec.tmp", "NIFTY_1030.trec.tmp"]
    s.compact_due(now=T + 900 + 10)                                       # 10:35: 1015 over + grace? 10:30+45s
    assert _files(tmp_path) == ["NIFTY_1015.tcol", "NIFTY_1030.trec.tmp"]


def test_restart_mid_slot_and_late_ticks_merge(tmp_path):
    s = store.TickStore(tmp_path, info, start=False, packer=store.pack)
    s.add("NIFTY", tk("nse_fo|1", T, 100.0))
    s.flush()
    s2 = store.TickStore(tmp_path, info, start=False, packer=store.pack)                     # recorder restarted in the same slot
    s2.add("NIFTY", tk("nse_cm|Nifty 50", T + 60, 25000.0))
    s2.add("NIFTY", tk("nse_fo|1", T + 61, 99.0))
    s2.flush()
    s2.compact_due(now=T + 3600)
    s2.add("NIFTY", tk("nse_fo|1", T + 62, 98.0))                         # a very late tick for that slot
    s2.flush()
    s2.compact_due(now=T + 3600)
    assert _files(tmp_path) == ["NIFTY_1015.tcol"]
    rows = store.read_day("2026-11-13", "NIFTY", tmp_path)[1]
    assert [(r[1], r[2]) for r in rows] == [("nse_fo|1", 100.0), ("nse_cm|Nifty 50", 25000.0),
                                            ("nse_fo|1", 99.0), ("nse_fo|1", 98.0)]


def test_torn_last_frame_skipped(tmp_path):
    s = store.TickStore(tmp_path, info, start=False, packer=store.pack)
    s.add("NIFTY", tk("nse_fo|1", T, 100.0))
    s.flush()
    p = tmp_path / "2026-11-13" / "NIFTY_1015.trec.tmp"
    p.write_bytes(p.read_bytes() + b"\x40\x00\x00\x00\x78\x9c")
    assert len(store.read_day("2026-11-13", "NIFTY", tmp_path)[1]) == 1
    s.compact_due(now=T + 3600)
    assert len(store.read_day("2026-11-13", "NIFTY", tmp_path)[1]) == 1


def _und(expiries, futs, strikes):
    chains = {e: {k: {"CE": NS(key=f"{e}|{k}|CE"), "PE": NS(key=f"{e}|{k}|PE")} for k in strikes} for e in expiries}
    return NS(expiries=lambda: expiries, futures=[NS(expiry=f, key=f"FUT|{f}") for f in futs],
              strikes=lambda e: strikes, chains=chains, spot_key="nse_cm|Nifty 50")


def test_expiries_monthly_taken_moves_to_next_month():
    u = _und(["2026-11-17", "2026-11-23", "2026-12-01", "2026-12-29"], ["2026-11-23", "2026-12-29"], [25000])
    assert S.pick_expiries(u) == ["2026-11-17", "2026-11-23", "2026-12-29"]
    u = _und(["2026-11-03", "2026-11-10", "2026-11-17", "2026-11-23"], ["2026-11-23", "2026-12-29"], [25000])
    assert S.pick_expiries(u) == ["2026-11-03", "2026-11-10", "2026-11-23"]


def test_strikes_within_band():
    u = _und(["2026-11-17", "2026-11-23", "2026-12-29"], ["2026-11-23", "2026-12-29"], [20000, 22100, 25000, 27900, 30000])
    keys = S.pick_keys(u, 25000.0)
    assert {k.split("|")[1] for k in keys} == {"22100", "25000", "27900"} and len(keys) == 3 * 3 * 2


def test_window():
    ist = S.IST
    assert S.in_window(dt.datetime(2026, 11, 13, 9, 0, tzinfo=ist))
    assert not S.in_window(dt.datetime(2026, 11, 13, 15, 40, tzinfo=ist))
    assert not S.in_window(dt.datetime(2026, 11, 14, 11, 0, tzinfo=ist))      # Saturday


def test_pack_in_separate_process_and_many_contracts(tmp_path):
    import random
    rnd = random.Random(1)
    s = store.TickStore(tmp_path, info, start=False)                      # default packer = subprocess
    want = []
    for j in range(5000):
        key = f"nse_fo|{rnd.randint(1, 300)}"
        px = round(rnd.uniform(0.05, 900) * 20) / 20
        t = T + j * 0.1
        s.add("SENSEX", tk(key, t, px, px - 0.05, px + 0.05, rnd.randint(1, 900), rnd.randint(0, 10**7)))
        want.append((round(t * 1000), key, px))
    s.flush()
    s.compact_due(now=T + 3600)
    assert s.stats["packed"] == 1 and s.stats["pack_errors"] == 0, s.stats
    assert _files(tmp_path) == ["SENSEX_1015.tcol"]
    rows = store.read_day("2026-11-13", "SENSEX", tmp_path)[1]
    assert [(round(r[0] * 1000), r[1], r[2]) for r in rows] == want
