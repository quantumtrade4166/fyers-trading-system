"""
cepe_imbalance/test_rest_backup.py - REST backup prices for the live engine while Kotak's stream is silent
(25-Nov-2026). Fake Kotak quotes(), offline.
"""
import time

from live_trading_options.cepe_imbalance import rest_backup as RB
from live_trading_options.cepe_imbalance.test_synthetic import _live_rig, universe  # noqa: F401


class _Sess:
    def __init__(self, client):
        self.client = client


class _Holder:
    def __init__(self, client):
        self.s = _Sess(client)

    def get(self, force=False):
        return self.s


class _Quotes:
    def __init__(self, g):
        self.g, self.calls = g, []

    def quotes(self, instrument_tokens, quote_type):
        self.calls.append(len(instrument_tokens))
        out = []
        for it in instrument_tokens:
            tok, seg = it["instrument_token"], it["exchange_segment"]
            key = f"{seg}|{tok}"
            if seg.endswith("_cm"):                          # index spot asked by NAME
                out.append({"exchange_token": tok, "ltp": str(self.g.f + 40)})
                continue
            c = self.g.u.by_key.get(key)
            if not c:
                continue
            bid, ask = self.g.k.px.get(c.symbol, (0, 0))
            out.append({"exchange_token": tok, "ltp": str((bid + ask) / 2 + 1),
                        "depth": {"buy": [{"price": str(bid), "quantity": "650"}], "sell": [{"price": str(ask), "quantity": "650"}]}})
        return out


def test_backup_prices_reach_the_book(universe):
    g = _live_rig(universe, "NIFTY_SYN2_LIVE")
    assert g.eng.start("NIFTY_SYN2_LIVE")["ok"]
    q = _Quotes(g)
    b = RB.RestBackup(g.eng, feed=None, sessions=_Holder(q))
    keys = b.needed()
    assert g.u.underlyings["NIFTY"].spot_key in keys
    held = [p.key for p in g.r.book.positions.values() if p.lots]
    assert set(held) <= set(keys) and len(keys) <= 1 + 2 * (2 * RB.STRIKES_EACH_SIDE + 1)
    ticks = b.poll(keys, now=time.time())
    assert all(len_ <= RB.CHUNK for len_ in q.calls)
    spot = next(t for t in ticks if t.key == g.u.underlyings["NIFTY"].spot_key)
    assert spot.ltp == g.f + 40 and spot.bid is None
    opt = next(t for t in ticks if t.key == held[0])
    assert opt.bid is not None and opt.ask is not None and opt.bid <= opt.ask
    before = g.m.quote(held[0]).ltp
    g.eng.on_ticks(ticks)                                     # the engine takes them like stream ticks
    assert g.m.quote(held[0]).ltp != before
