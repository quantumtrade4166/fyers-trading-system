"""
cepe_imbalance/replay_archive.py — replay the BTC CE/PE books on the Delta 1-minute chain archive.
==================================================================================================

Source: live_trading_options/delta_btc/data/chain_archive/{date}/chunk_*.parquet (whole BTC option chain,
real best bid / ask, every 60 s, IST timestamps), trimmed per day to the expiries D and D+1 into
`{date}.parquet` files (see the extract step in the 15-Nov session). Each snapshot becomes one tick per
contract (ltp = mark, bid / ask / sizes) + one spot tick; replay_rules.run() applies a whole snapshot and
then evaluates the rules once, i.e. the imbalance is checked once a minute (live: every tick).

  BTC      trading day D = 09:00 -> 17:20 IST of D           (rows of file D)
  BTCFULL  trading day D = 17:30 of D-1 -> 17:20 of D         (rows of files D-1 and D)
"""
import datetime as dt
import math
from pathlib import Path

import pandas as pd

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import crypto
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import instruments as I
from live_trading_options.cepe_imbalance.instruments import Contract

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _iso(ddmmyy: str) -> str:
    return dt.datetime.strptime(ddmmyy, "%d%m%y").date().isoformat()


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or x <= 0 else x


def load(folder: Path, und: str, day: str):
    """-> (universe, rows) for one trading day of `und`, or (None, None) when the data is missing."""
    d = dt.date.fromisoformat(day)
    o, _close, sq = EN.session(und, d)
    files = sorted({o.date().isoformat(), d.isoformat()})
    parts = [pd.read_parquet(folder / f"{f}.parquet") for f in files if (folder / f"{f}.parquet").exists()]
    if not parts:
        return None, None
    df = pd.concat(parts, ignore_index=True)
    df = df[df["expiry"] == d.strftime("%d%m%y")]                     # the expiry this trading day sells
    if df.empty:
        return None, None
    ts = df["captured"].dt.tz_localize(IST).map(lambda x: x.timestamp())
    df = df.assign(t=ts)
    df = df[(df.t >= o.timestamp() - 1800) & (df.t <= sq.timestamp() + 60)].sort_values(["t", "symbol"])
    if df.empty:
        return None, None
    u = I.build({})
    cs = {}
    for sym, kind, k, e in df[["symbol", "opt_type", "strike", "expiry"]].drop_duplicates("symbol").itertuples(index=False):
        cs[sym] = Contract(key=crypto.PREFIX + sym, seg="delta", token=0, symbol=sym, name="BTC", kind=kind,
                           expiry=_iso(e), strike=float(k), lot=C.DELTA_LOT, tick=0.1, mult=0.001)
    for n in crypto.names():
        x = u.underlyings[n]
        x.settle, x.lot, x.mult, x.chains = C.CRYPTO_SETTLE, C.DELTA_LOT, 0.001, {}
        for c in cs.values():
            u.by_key[c.key] = c
            x.chains.setdefault(c.expiry, {}).setdefault(c.strike, {})[c.kind] = c
        for e in list(x.chains):
            x.chains[e] = {k: v for k, v in sorted(x.chains[e].items()) if "CE" in v and "PE" in v}
    rows = []
    for t, g in df.groupby("t", sort=True):
        sp = _num(g["spot"].iloc[0])
        if sp:
            rows.append([t, crypto.SPOT_KEY, sp, None, None, None, None, None, None])
        for sym, mark, bid, ask, bq, aq in g[["symbol", "mark", "best_bid", "best_ask", "bid_size", "ask_size"]].itertuples(index=False):
            m = _num(mark)
            if m is None:
                continue
            b, a = _num(bid), _num(ask)
            rows.append([t, crypto.PREFIX + sym, m, b, a, int(bq) if b and bq == bq else None,
                         int(aq) if a and aq == aq else None, None, None])
    return u, rows


def days(folder: Path, und: str) -> list:
    """Trading days with a full session in the trimmed archive."""
    have = {p.stem for p in folder.glob("*.parquet")}
    out = []
    for h in sorted(have):
        d = dt.date.fromisoformat(h)
        o, _c, sq = EN.session(und, d)
        if o.date().isoformat() in have and d.isoformat() in have:
            out.append(h)
    return out
