"""
core/fills.py — what a real market order would have been filled at.
====================================================================

A real market order does not get "the price". It takes the best level, and if that
level is too small for the order it takes the next one, and the next, until the
whole size is done. The price you actually pay is the size-weighted average of
every level it ate through.

    SELL 1000 into bids   40.0 x 600  |  39.5 x 300  |  39.0 x 5000
      -> 600 @ 40.0 + 300 @ 39.5 + 100 @ 39.0  =  avg 39.75, not 40.0

That is what this computes, from the exchange's live L2 book, for every paper
entry, exit and stop-loss — so the paper book pays what real money would pay.

Pure functions: no network, no clock. The chain fetches the book; this walks it.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def walk_book(levels, qty: int) -> dict:
    """Fill `qty` contracts against `levels`, best price first.

    `levels` is the side of the book the order CROSSES, already in exchange order:
    bids high-to-low for a SELL, asks low-to-high for a BUY. Each level is a dict
    with `price` and `size` (Delta's L2 shape) or a (price, size) pair.

    Returns:
      price        size-weighted average fill price — the number to book
      top          the best level's price, for comparison in the log
      worst        the last (worst) level the order reached
      levels_used  how many levels it ate through
      filled       contracts the visible book could absorb
      short        contracts it could NOT absorb (0 when the book was deep enough)

    WHEN THE BOOK RUNS OUT. If the visible book holds fewer contracts than the
    order, the remainder is priced at the worst level reached and `short` says how
    many. A real market order would either sweep deeper (worse still) or be
    partially cancelled; booking the remainder at the worst visible price is the
    least optimistic assumption available without inventing prices that were never
    on the screen, and the flag makes every such fill visible in the log.
    """
    qty = int(qty)
    got, cost, used, worst, top = 0, 0.0, 0, None, None
    for lv in levels or []:
        if isinstance(lv, dict):
            px, sz = _f(lv.get("price")), _f(lv.get("size"))
        else:
            px, sz = _f(lv[0]), _f(lv[1])
        if px is None or sz is None or sz <= 0:
            continue
        if top is None:
            top = px
        take = min(int(sz), qty - got)
        if take <= 0:
            break
        cost += take * px
        got += take
        used += 1
        worst = px
        if got >= qty:
            break
    if got == 0:
        return {"price": None, "top": None, "worst": None, "levels_used": 0,
                "filled": 0, "short": qty}
    short = qty - got
    if short > 0:
        cost += short * worst
    return {"price": round(cost / qty, 4), "top": top, "worst": worst,
            "levels_used": used, "filled": got, "short": short}
