"""
cepe_imbalance/strategy.py — where the owner's CE/PE imbalance rules plug in.
=============================================================================

One Strategy instance per underlying. The engine calls, on the feed thread, in order:
  on_start(ctx)          once when the underlying is started (Start button / auto-start)
  on_tick(ctx, keys)     every batch of ticks that touched this underlying (its options, future or spot)
  on_timer(ctx)          once a second while running
  on_session_end(ctx)    at the configured square-off time (default: exit everything)

`ctx` (engine.Context) gives:
  ctx.name, ctx.params, ctx.now (IST datetime), ctx.expiry (chosen expiry)
  ctx.ref_price(), ctx.atm(), ctx.contract(strike, "CE"|"PE"), ctx.quote(key), ctx.window(n)
  ctx.book.order(contract, "BUY"|"SELL", lots, reason), ctx.book.exit_all(reason)
  ctx.book.mtm(), ctx.book.imbalance(), ctx.book.positions
  ctx.state (dict that survives restarts the same day), ctx.log(event, **fields)

RULES ARE NOT WRITTEN YET — the owner will give them. Until then `Idle` does nothing (manual paper
orders from the screen still work, so imbalance/MTM can be checked live).
"""


class Strategy:
    params: dict = {}

    def on_start(self, ctx):
        pass

    def on_tick(self, ctx, keys):
        pass

    def on_timer(self, ctx):
        pass

    def on_session_end(self, ctx):
        ctx.book.exit_all("session end")


class Idle(Strategy):
    """Placeholder until the owner's rules arrive."""


REGISTRY = {"idle": Idle}
DEFAULT = "idle"
