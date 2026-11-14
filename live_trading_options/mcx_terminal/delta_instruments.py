"""
mcx_terminal/delta_instruments.py — the BTC desk's contract universe from Delta Exchange India.
==============================================================================================

Same shapes as instruments.py (Universe / Product / Contract), built from Delta's PUBLIC product list
(/v2/products, no key). Checked 15-Nov-2026:
  - BTC options: symbol C-BTC-65000-151126 / P-BTC-…, product `id` (our token), `strike_price`,
    `settlement_time` 12:00 UTC = 17:30 IST (daily, weekly, monthly, quarterly expiries),
    `contract_value` 0.001 (BTC per contract), `tick_size` 0.1 (USD)
  - options settle on the index .DEXBTUSD -> the chain's underlying is that index (Product.spot, token
    config.BTC_SPOT_TOKEN), never a future
Quantities are CONTRACTS (lot = 1). mult = USD per 1 point x 1 contract = contract_value (0.001).
"""
import datetime as dt
import json
import logging

from live_trading_options.mcx_terminal import config as C
from live_trading_options.mcx_terminal.instruments import Contract, Product, Universe

log = logging.getLogger("mcx.delta_instruments")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SEG = "delta"


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _settle(r) -> "dt.datetime | None":
    s = r.get("settlement_time") or ""
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse(rows: list, now: "dt.datetime | None" = None) -> Universe:
    """Delta product rows -> Universe with one product, BTC. Expired / not-yet-settled-but-past rows dropped."""
    now = now or dt.datetime.now(dt.timezone.utc)
    p = Product("BTC", lot=1)
    by_token = {}
    for r in rows:
        ct = r.get("contract_type")
        if ct not in ("call_options", "put_options"):
            continue
        und = (r.get("underlying_asset") or {}).get("symbol") or r.get("underlying_asset_symbol")
        if und != "BTC" or r.get("state", "live") != "live":
            continue
        st = _settle(r)
        if st is None or st <= now:
            continue
        expiry = st.astimezone(IST).date().isoformat()
        kind = "CE" if ct == "call_options" else "PE"
        c = Contract(token=int(r["id"]), symbol=r.get("symbol", ""), name="BTC", kind=kind, expiry=expiry,
                     strike=round(_f(r.get("strike_price")), 2), lot=1, tick=_f(r.get("tick_size"), 0.1) or 0.1,
                     freeze=int(_f(r.get("position_size_limit"), 0)), mult=_f(r.get("contract_value"), 0.001) or 0.001,
                     seg=SEG)
        by_token[c.token] = c
        p.opt_tick = p.opt_tick or c.tick
        p.chains.setdefault(expiry, {}).setdefault(c.strike, {})[kind] = c
    for e in list(p.chains):
        p.chains[e] = {k: v for k, v in sorted(p.chains[e].items()) if "CE" in v and "PE" in v}
        if not p.chains[e]:
            del p.chains[e]
    p.spot = Contract(token=C.BTC_SPOT_TOKEN, symbol=".DEXBTUSD", name="BTC", kind="IDX", expiry="", strike=0.0,
                      lot=1, tick=0.1, seg=SEG)
    by_token[p.spot.token] = p.spot
    return Universe(products={"BTC": p}, by_token=by_token)


def load(client=None) -> Universe:
    """Today's Delta contract list (public REST), cached to data_btc/products_<day>.json. `client` unused
    (same call shape as instruments.load)."""
    from live_trading_options.delta_btc.core import api
    day = C.day()
    cache = C.DATA / f"products_{day}.json"
    rows = None
    try:
        rows = api.products()                      # live call + put options, every page
        cache.write_text(json.dumps(rows), encoding="utf-8")
        for old in C.DATA.glob("products_*.json"):
            if old != cache:
                old.unlink(missing_ok=True)
    except Exception as e:
        if not cache.exists():
            raise
        log.error("Delta product list not reachable (%s) - using today's cached copy", e)
        rows = json.loads(cache.read_text(encoding="utf-8"))
    u = parse(rows)
    u.file_date = day
    p = u.products["BTC"]
    log.info("BTC: %d option expiries (%s), %d contracts", len(p.chains), ", ".join(p.expiries()[:6]),
             len(u.by_token) - 1)
    return u
