"""
core/depth.py — executable price for a SIZE, by walking the book (for v2).
=========================================================================

v1 prices everything at the top of book and only checks that the top level
holds TRADE_SIZE_BTC (spread_engine.size_ok). When the L2 feeds are wired in
(Delta `l2_orderbook`, Binance `<symbol>@depth20@100ms`), the spread for a size
becomes:

    s1(size) = walk(binance_bids, size) - walk(delta_asks, size)
    s2(size) = walk(delta_bids, size)   - walk(binance_asks, size)

and spread_engine.compute() takes these averages in place of the top prices —
nothing else in the pipeline changes.

Example from the prompt: Delta asks [(100000, 0.003), (100005, 0.010)], buying
0.01 BTC -> 0.003 @ 100000 + 0.007 @ 100005 = average 100003.5, not 100000.
"""


def walk(levels, size_btc: float):
    """levels: [(price, qty_btc), ...] best first. Returns the size-weighted
    average price to fill `size_btc`, or None if the book is too thin."""
    if size_btc <= 0:
        return None
    got = cost = 0.0
    for price, qty in levels:
        take = min(qty, size_btc - got)
        if take <= 0:
            continue
        got += take
        cost += take * price
        if got >= size_btc - 1e-12:
            return cost / got
    return None
