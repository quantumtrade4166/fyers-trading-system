"""
live/kotak_executor.py — Kotak Neo order placement for the strangle MIRROR leg.
==============================================================================

The ONLY module that sends orders to Kotak. Mirrors the kite_executor interface so the
controller can drive either broker. Symbol resolution comes from Kotak's own scrip list
(search_scrip) — never a hand-built symbol.

Verified against the live API (login + search_scrip + method sources), 2026-09-02:
  - search_scrip(exchange_segment, symbol, option_type, strike_price) -> list of dicts:
      pTrdSymbol="NIFTY2690824250CE", pSymbolName="NIFTY", pOptionType="ce",
      "dStrikePrice;"=<strike*100>, pExpiryDate="08Sep2026", lLotSize=65, pSymbol=<token>,
      dTickSize=5 (=0.05)
  - place_order(exchange_segment, product, price, order_type, quantity, validity,
      trading_symbol, transaction_type, tag=...)  — order_type "L", transaction_type "B"/"S".

NOT YET observed live (there were no orders to read): the exact SUCCESS shape of
place_order and of order_report-with-data. Those paths log the RAW response and parse it
defensively across the known Kotak key spellings, so the first real order is self-documenting
rather than silently mis-read.
"""

import datetime as dt

SEGMENT = {"NIFTY": "nse_fo", "SENSEX": "bse_fo"}     # Kotak exchange_segment per index
INDEX_NAME = {"NIFTY": "NIFTY", "SENSEX": "SENSEX"}   # exact pSymbolName to match
BUY, SELL = "B", "S"                                  # Kotak transaction_type codes
PRODUCT_NRML = "NRML"
ORDER_TYPE_LIMIT = "L"
VALIDITY_DAY = "DAY"
_TICK = 0.05


def _opt(t: str) -> str:
    return "ce" if str(t).upper().startswith("C") else "pe"


def _round_tick(price: float, tick: float = _TICK) -> float:
    return round(round(price / tick) * tick, 2)


def _expiry_str(expiry) -> str:
    """Our expiry (date / datetime / 'YYYY-MM-DD') -> Kotak's 'DDMonYYYY' (e.g. 08Sep2026)."""
    if isinstance(expiry, str):
        expiry = dt.date.fromisoformat(expiry[:10])
    return expiry.strftime("%d%b%Y")


# ── symbol resolution (cached per process; the scrip list is a big daily dump) ──
_scrip_cache: dict = {}


def _scrips(client, seg: str, name: str, opt: str) -> list:
    key = (seg, name, opt)
    if key not in _scrip_cache:
        rows = client.search_scrip(exchange_segment=seg, symbol=name,
                                   option_type=opt.upper(), strike_price="")
        if not isinstance(rows, list):
            raise RuntimeError(f"Kotak search_scrip failed ({seg} {name} {opt}): {rows}")
        _scrip_cache[key] = [r for r in rows
                             if r.get("pSymbolName") == name
                             and str(r.get("pOptionType", "")).lower() == opt]
    return _scrip_cache[key]


def resolve(client, index: str, expiry, strike: int, opt_type: str) -> dict:
    """Exact Kotak contract for (index, expiry, strike, CE/PE). Raises if no exact match
    (better to fail loudly than mirror onto a guessed symbol).
    Returns {trading_symbol, exchange_segment, lot_size, token}."""
    seg, name, opt = SEGMENT[index], INDEX_NAME[index], _opt(opt_type)
    want_exp, want_strike = _expiry_str(expiry), float(strike)
    for r in _scrips(client, seg, name, opt):
        rs = r.get("dStrikePrice;", r.get("dStrikePrice"))
        if rs is None:
            continue
        if abs(float(rs) / 100.0 - want_strike) > 0.5:
            continue
        if str(r.get("pExpiryDate", "")).replace(" ", "") != want_exp:
            continue
        return {"trading_symbol": r["pTrdSymbol"], "exchange_segment": seg,
                "lot_size": int(r.get("lLotSize") or r.get("iLotSize") or 0),
                "token": r.get("pSymbol")}
    raise RuntimeError(f"no Kotak contract: {index} {want_exp} {int(strike)} {opt}")


# ── marketable-limit pricing (mark-based; Kotak feed not needed for the mirror) ──
_MKT_BUF = 0.30


def marketable_limit(mark: float, side: str, buf: float = _MKT_BUF) -> float:
    """A limit priced THROUGH the touch off the strategy's own mark (from Fyers), so it
    fills like a market order. Only a worst-case cap. SELL below, BUY above."""
    ref = float(mark or 0)
    if ref <= 0:
        return 0.05 if side == SELL else 100000.0
    return _round_tick(ref * (1 - buf) if side == SELL else ref * (1 + buf))


# ── order id / status parsing (defensive across Kotak's key spellings) ──
_OID_KEYS = ("nOrdNo", "orderId", "order_id", "OrderNumber", "orderNumber", "ordNo")


def _dig(d: dict, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def _extract_oid(resp) -> str | None:
    if not isinstance(resp, dict):
        return None
    oid = _dig(resp, *_OID_KEYS)
    if oid:
        return str(oid)
    data = resp.get("data")
    if isinstance(data, dict):
        oid = _dig(data, *_OID_KEYS)
        return str(oid) if oid else None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        oid = _dig(data[0], *_OID_KEYS)
        return str(oid) if oid else None
    return None


def place_limit(client, trading_symbol: str, exchange_segment: str, side: str, qty: int,
                price: float, product: str = PRODUCT_NRML, tag: str = "vwstk_kotak") -> str:
    """Place a marketable LIMIT order and return its order id. ⚠️ REAL ORDER.
    Logs the raw response the first time so the success shape is captured."""
    resp = client.place_order(
        exchange_segment=exchange_segment, product=product,
        price=str(_round_tick(price)), order_type=ORDER_TYPE_LIMIT,
        quantity=str(int(qty)), validity=VALIDITY_DAY, trading_symbol=trading_symbol,
        transaction_type=side, tag=tag)
    if isinstance(resp, dict) and resp.get("error"):
        raise RuntimeError(f"Kotak place_order error: {resp.get('error')}")
    oid = _extract_oid(resp)
    if not oid:
        raise RuntimeError(f"Kotak place_order: no order id in response: {resp}")
    return oid


def _order_rows(client) -> list:
    rep = client.order_report()
    if isinstance(rep, dict):
        data = rep.get("data")
        if isinstance(data, list):
            return data
        return []                                    # {'errMsg': 'No Data'} etc.
    return rep if isinstance(rep, list) else []


_STATUS_KEYS = ("ordSt", "orderStatus", "status", "stat")
_FILLED_KEYS = ("fldQty", "filledQty", "filled_quantity", "fillQty")
_AVG_KEYS = ("avgPrc", "averagePrice", "avg_price", "avgPrice")
_SYM_KEYS = ("trdSym", "tradingSymbol", "trading_symbol", "sym", "pTrdSymbol")
_SIDE_KEYS = ("trnsTp", "transactionType", "transaction_type", "buySell")
_QTY_KEYS = ("qty", "quantity", "orderQty")
_TIME_KEYS = ("flDtTm", "fillTime", "exchTime", "ordDtTm", "orderTime")


def order_status(client, order_id: str) -> dict:
    """{status, filled_qty, avg_price, fill_time} for one order id, parsed defensively.
    A read failure returns status=None so a poll loop retries rather than crashing."""
    try:
        for o in _order_rows(client):
            if str(_dig(o, *_OID_KEYS)) == str(order_id):
                return {"status": _dig(o, *_STATUS_KEYS),
                        "filled_qty": int(_dig(o, *_FILLED_KEYS) or 0),
                        "avg_price": _dig(o, *_AVG_KEYS),
                        "fill_time": _dig(o, *_TIME_KEYS)}
    except Exception:
        pass
    return {"status": None, "filled_qty": 0, "avg_price": None, "fill_time": None}


def cancel(client, order_id: str):
    try:
        return client.cancel_order(order_id=str(order_id))
    except Exception as e:
        return {"error": str(e)}


def strategy_fills(client, tag: str = "vwstk_kotak") -> list:
    """Today's COMPLETE orders THIS mirror placed, by our tag — the own-book source for
    reconciliation after a restart. Never uses positions() (netted, mixes manual trades)."""
    out = []
    for o in _order_rows(client):
        st = str(_dig(o, *_STATUS_KEYS) or "").lower()
        otag = _dig(o, "tag", "orderTag")
        if otag != tag or ("complete" not in st and "traded" not in st and st != "filled"):
            continue
        out.append({"trading_symbol": _dig(o, *_SYM_KEYS), "side": _dig(o, *_SIDE_KEYS),
                    "qty": int(_dig(o, *_FILLED_KEYS) or _dig(o, *_QTY_KEYS) or 0),
                    "avg_price": _dig(o, *_AVG_KEYS), "order_id": str(_dig(o, *_OID_KEYS)),
                    "fill_time": _dig(o, *_TIME_KEYS)})
    return out
