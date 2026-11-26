"""
cepe_imbalance/test_market_behind.py - two feeds (Kotak + Kite) for one contract, one ~5 s behind
(26-Nov-2026: SENSEX 84400 CE flipped 231 <-> 241 every few hundred ms, paper MTM spiked +/-5k).

    .venv\Scripts\python.exe -m pytest live_trading_options/cepe_imbalance/test_market_behind.py -q
"""
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market, behind_volume

K = "bse_fo|824110"
T0 = 1795669836.0


def tick(ltp, vol, t, exch=None):
    return Tick(K, ltp, ltp - 0.5, ltp + 0.5, 20, 20, vol, 2770760, int(exch or t), t)


def test_the_26_nov_trace_keeps_only_the_newer_stream():
    m = Market(universe=None)
    seq = [(228.6, 48493000), (238.45, 48528080), (230.0, 48497020), (239.5, 48543020),
           (230.5, 48503360), (241.3, 48551740), (231.1, 48503600), (241.8, 48564820), (231.0, 48514320)]
    seen = []
    for i, (p, v) in enumerate(seq):
        m.apply([tick(p, v, T0 + i * 0.3)])
        seen.append(m.quote(K).ltp)
    assert seen == [228.6, 238.45, 238.45, 239.5, 239.5, 241.3, 241.3, 241.8, 241.8]
    assert m.behind_dropped == 4


def test_equal_volume_or_missing_volume_is_never_dropped():
    m = Market(universe=None)
    m.apply([tick(240.0, 1000, T0)])
    m.apply([tick(241.0, 1000, T0 + 1)])            # same volume, new quote: kept
    m.apply([Tick(K, 242.0, None, None, None, None, None, None, int(T0 + 2), T0 + 2)])  # LTP-only packet
    assert m.quote(K).ltp == 242.0 and m.behind_dropped == 0


def test_units_mismatch_between_sources_never_blocks_a_feed():
    # one source in lots, the other in units: far apart, never treated as "behind"
    assert not behind_volume(745_000, 48_500_000, T0, T0 + 1)


def test_new_day_volume_restarts_low():
    assert not behind_volume(100, 48_500_000, T0, T0 + 3600)       # held quote is an hour old
    assert behind_volume(48_400_000, 48_500_000, T0, T0 + 1)
