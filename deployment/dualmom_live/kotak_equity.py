"""
dualmom_live/kotak_equity.py — Kotak Neo NSE CASH (CNC) order layer for DualMom.

⚠️ INERT. No module here logs in, and nothing calls a Kotak endpoint unless a
caller passes a live client. Built 2026-09-07 with the market open and the Vwap
Strangle mirror holding the account's single Kotak session.

WHY THIS IS NOT live/kotak_executor.py
--------------------------------------
That module is the options mirror: `nse_fo`/`bse_fo`, product NRML, strike/expiry
resolution via search_scrip(option_type=...). DualMom is NSE cash, product CNC,
plain equity symbols, and needs holdings() + limits() which options never used.

The proven response-parsing helpers below are COPIED, not imported, on purpose.
Live Order Safety: "two strategies must never share a tag" — and by the same
logic they should not share code whose change would move both books at once.
A break in the strangle's executor must never reach DualMom, or vice versa.

SESSION HAZARD (read before wiring anything up)
-----------------------------------------------
Kotak Neo permits ONE active session per account. `live_tick_engine._get_kotak_client()`
logs in at 09:20 and caches for the day. A second login STEALS that session and the
strangle would lose its 15:14 square-off. So every function here takes `client` as
its first argument and this module has no login() of its own. Either:
  (a) run inside the dashboard process and pass `_get_kotak_client()`, or
  (b) run after ~15:20 once the strangle is flat and its session is idle, or
  (c) use a separate Kotak account.

Order-safety rules carried over from [[Live Order Safety]]:
  1. never write a fill you have not seen  -> only order_status() closes a leg
  2. a rejection is DATA, not an exception -> place_* return status dicts
  3. tag every order and scope the book to that tag
  4. equity DOES accept market orders (unlike Zerodha options) but we still use
     a marketable LIMIT: 40 orders into thin momentum names would slip badly
"""

import time
from datetime import datetime

SEGMENT = "nse_cm"
PRODUCT = "CNC"
BUY, SELL = "B", "S"


# ── response helpers (mirrored from the proven live/kotak_executor.py) ───────

def _txn_code(side: str) -> str:
    """Kotak's client-side validator accepts ONLY {'B','S','Buy','Sell'}.

    Passing the ledger's 'BUY'/'SELL' words is what broke the first live Kotak
    order on 2026-09-03 (ApiValueError). Normalise here, once.
    """
    s = str(side).strip().upper()
    if s in ("S", "SELL"):
        return SELL
    if s in ("B", "BUY"):
        return BUY
    raise ValueError(f"unknown side {side!r}")


def _dig(d, *keys):
    """First present, non-empty value among `keys`, searched one level deep.
    Kotak spells the same field differently across endpoints."""
    if not isinstance(d, dict):
        return None
    for k in keys:
        if d.get(k) not in (None, "", []):
            return d[k]
    for v in d.values():
        if isinstance(v, dict):
            got = _dig(v, *keys)
            if got is not None:
                return got
        elif isinstance(v, list):
            for item in v:
                got = _dig(item, *keys) if isinstance(item, dict) else None
                if got is not None:
                    return got
    return None


def _extract_oid(resp):
    """Order id out of a place_order reply. Verified shape on 2026-09-07 via a
    real dry order: id came back as '260907000049474'."""
    return _dig(resp, "nOrdNo", "orderId", "order_id", "ordNo", "nordno", "Ordno")


def _err(resp):
    """Kotak returns error DICTS instead of raising. Returns a message or None."""
    if not isinstance(resp, dict):
        return None if resp else "empty response"
    e = resp.get("error") or resp.get("Error Message") or resp.get("errMsg")
    if isinstance(e, list) and e:
        return str(_dig(e[0], "message", "msg", "errMsg") or e[0])
    return str(e) if e else None


def _num(x, default=0.0):
    try:
        return float(str(x).replace(",", "").strip())
    except Exception:
        return default


def is_margin_error(msg: str) -> bool:
    """Shared classifier — out of funds must flatten and stop, not retry blindly."""
    m = (msg or "").lower()
    return any(k in m for k in ("insufficient fund", "margin required", "margin available",
                                "insufficient margin", "margin shortfall", "insufficient balance"))


def round_tick(price: float, tick: float = 0.05) -> float:
    """Round to the instrument's OWN tick.

    ⚠️ The default is a fallback, not a norm. Verified live 2026-09-08, nse_cm
    ticks in our own 40-name basket span 0.01 to 5.00:
        HFCL 0.01 | most names 0.05 | RELIANCE 0.10 | NETWEB 0.50 |
        BAJAJ-AUTO 1.00 | POWERINDIA 5.00
    Sending 33,928.80 for a 5.00-tick stock is an exchange-level rejection, so
    the resolved tick MUST be threaded through from resolve()['tick_size'].
    """
    t = float(tick) if tick else 0.05
    return round(round(float(price) / t) * t, 2)


def marketable_limit(mark: float, side: str, buf: float = 0.005,
                     tick: float = 0.05) -> float:
    """Price THROUGH the touch so a limit behaves like a market order.
    A marketable limit fills at the opposite side, not at your limit — the limit
    is a cap, not the price paid."""
    s = _txn_code(side)
    px = mark * (1 + buf) if s == BUY else mark * (1 - buf)
    t = float(tick) if tick else 0.05
    return max(round_tick(px, t), t)


# ── reads ────────────────────────────────────────────────────────────────────

import weakref as _weakref
from datetime import date as _date

_resolve_cache = _weakref.WeakKeyDictionary()   # client -> {(symbol, day): info}


def resolve(client, symbol: str, retries: int = 3) -> dict:
    """Cached per client, per day. Only successes are cached.

    One dashboard Preview resolved each of 40 names two or three times through
    search_scrip (plan, live prices, UI annotation), and execute() resolves them
    all again - hundreds of round trips that pushed Preview past the proxy's
    10-minute timeout. A trading symbol, series and tick do not change intraday,
    but a stock CAN change series between days (HFCL moved EQ -> BE), so the
    cache key includes the date.
    """
    key = (symbol.strip().upper(), _date.today())
    try:
        per_client = _resolve_cache.setdefault(client, {})
    except TypeError:                                  # client not weak-referenceable
        return _resolve_uncached(client, symbol, retries)
    hit = per_client.get(key)
    if hit is not None:
        return hit
    info = _resolve_uncached(client, symbol, retries)
    per_client[key] = info
    return info


def _resolve_uncached(client, symbol: str, retries: int = 3) -> dict:
    """Map an NSE symbol (e.g. 'RELIANCE') to Kotak's cash trading symbol.

    VERIFIED against the live nse_cm scrip master 2026-09-08. Real shapes:
        RELIANCE   -> pTrdSymbol 'RELIANCE-EQ'   pGroup 'EQ'  dTickSize 10
        HFCL       -> pTrdSymbol 'HFCL-BE'       pGroup 'BE'  dTickSize 1
        BAJAJ-AUTO -> pTrdSymbol 'BAJAJ-AUTO-EQ' pGroup 'EQ'  dTickSize 100
    So dTickSize is in PAISE and the trading symbol carries the SERIES suffix.

    ⚠️ EXACT NAME MATCH IS MANDATORY. Kotak's search_scrip is a SUBSTRING search
    and does not guarantee the symbol you asked for is even in the results:
    searching 'LTIM' returns 'EMULTIMQ' and 'ALLTIME' -- two unrelated scrips that
    merely contain those letters. A loose "take the first row" picker would have
    bought a random stock with real money. If no row's pSymbolName equals the
    request exactly, we refuse.
    """
    last = None
    for attempt in range(1, retries + 1):
        try:
            rows = client.search_scrip(exchange_segment=SEGMENT, symbol=symbol)
            break
        except Exception as e:                     # transient ConnectionError seen live
            last = e
            if attempt == retries:
                raise RuntimeError(f"search_scrip {symbol}: {type(e).__name__}: {e}")
            time.sleep(1.5 * attempt)
    if isinstance(rows, dict):
        e = _err(rows)
        if e:
            raise RuntimeError(f"search_scrip {symbol}: {e}")
        rows = rows.get("data") or []

    want = symbol.strip().upper()
    best = None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        name = str(_dig(r, "pSymbolName", "pScripRefKey") or "").strip().upper()
        series = str(_dig(r, "pGroup", "pSeries", "series") or "").strip().upper()
        # EQ = normal rolling settlement. BE = trade-for-trade (compulsory
        # delivery, no netting) -- still valid for a CNC delivery book, but the
        # caller is told so it can flag it. Anything else (SM/ST/IL/GB) is not
        # something this strategy should be buying.
        if name == want and series in ("EQ", "BE"):
            if best is None or series == "EQ":     # prefer EQ if both exist
                best = r
    if best is None:
        seen = sorted({str(_dig(r, "pSymbolName", "") or "") for r in (rows or [])
                       if isinstance(r, dict)})[:5]
        raise LookupError(
            f"no exact nse_cm EQ/BE match for {symbol!r} "
            f"({len(rows or [])} rows returned{', e.g. ' + ', '.join(seen) if seen else ''})")

    series = str(_dig(best, "pGroup", "pSeries") or "EQ").strip().upper()
    tick_paise = _num(_dig(best, "dTickSize"), 5)
    return {
        "symbol": symbol,
        "trading_symbol": _dig(best, "pTrdSymbol", "tradingSymbol") or symbol,
        "exchange_segment": SEGMENT,
        "token": _dig(best, "pSymbol", "token"),
        "series": series,
        "is_trade_for_trade": series == "BE",
        "tick_size": (tick_paise / 100.0) if tick_paise else 0.05,
        "isin": _dig(best, "pISIN"),
        "raw": best,
    }


def last_prices(client, symbols) -> dict:
    """{symbol: last traded price} for many names in ONE quotes() call.

    VERIFIED against the live nse_cm API 2026-09-10. quotes() returns a **LIST**
    of row dicts (not a dict), each shaped:

        {"exchange_token": "2885", "display_symbol": "RELIANCE-EQ",
         "ltp": "1263.0000", "ohlc": {"open":..., "close": "1274.0000"}, ...}

    and this SDK build rejects `isIndex` / `quote_type` kwargs entirely.

    The previous version assumed a dict and called .get() on the list, so it
    raised AttributeError for EVERY symbol. The 15:25 stop check swallows that and
    falls back to the entry price — which can never breach — so the -35% stop
    would silently never have fired. A stop you believe in but that cannot trigger
    is worse than no stop, hence the batch call and the explicit token matching.
    """
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return {}

    token_of, want = {}, []
    for sym in syms:
        try:
            info = resolve(client, sym)
        except Exception:
            continue                     # unresolvable -> simply no mark
        tok = str(info["token"])
        token_of[tok] = sym
        want.append({"instrument_token": tok, "exchange_segment": SEGMENT})
    if not want:
        return {}

    out = {}
    # quotes() is capped per call in practice; 40 names is fine but chunk anyway
    for i in range(0, len(want), 25):
        chunk = want[i:i + 25]
        rows = None
        for attempt in range(1, 4):
            try:
                rows = client.quotes(instrument_tokens=chunk)
                break
            except Exception:
                time.sleep(1.0 * attempt)
        if rows is None:
            continue
        if isinstance(rows, dict):                    # defensive: shape may change
            e = _err(rows)
            if e:
                continue
            rows = rows.get("data") or rows.get("message") or []
            if isinstance(rows, dict):
                rows = [rows]
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            tok = str(_dig(r, "exchange_token", "instrument_token") or "")
            sym = token_of.get(tok)
            if sym is None:
                ds = str(_dig(r, "display_symbol", "trading_symbol") or "").upper()
                base = ds.rsplit("-", 1)[0]
                sym = next((x for x in syms if x.upper() == base), None)
            if sym is None:
                continue
            px = _num(_dig(r, "ltp", "last_traded_price", "lastPrice"), 0.0)
            if px <= 0:
                ohlc = r.get("ohlc")
                if isinstance(ohlc, dict):
                    px = _num(_dig(ohlc, "close"), 0.0)   # pre-open: use the close
            if px > 0:
                out[sym] = px
    return out


def last_price(client, symbol: str) -> float:
    """Single-symbol convenience over last_prices(). Raises if there is no mark."""
    got = last_prices(client, [symbol])
    if symbol not in got:
        raise RuntimeError(f"no usable price from quotes() for {symbol}")
    return got[symbol]


def holdings(client) -> dict:
    """{symbol: {'qty': int, 'avg_price': float}} for the whole demat account.

    ⚠️ Holdings carry NO tag — order tags do not survive into holdings. This is
    only a true DualMom book because the account is dedicated (config.
    ACCOUNT_IS_DEDICATED). If that ever stops being true, the own-book ledger
    becomes mandatory instead of a cross-check.
    """
    resp = client.holdings()
    e = _err(resp)
    # A brand-new or fully-sold account legitimately has nothing. Kotak reports
    # that as an ERROR ("No holdings found for this client <UCC>"), which would
    # have crashed the very FIRST rebalance -- the one where holdings are empty
    # by definition. Treat every flavour of "nothing there" as an empty book.
    if e and not any(t in e.lower() for t in
                     ("no data", "no holdings", "not found", "no record")):
        raise RuntimeError(f"holdings: {e}")
    rows = resp.get("data") if isinstance(resp, dict) else resp
    out = {}
    for r in rows or []:
        sym = str(_dig(r, "displaySymbol", "symbol", "trdSym", "instrumentName") or "").strip().upper()
        sym = sym.replace("-EQ", "")
        qty = int(_num(_dig(r, "quantity", "holdingCost", "sellableQuantity", "qty")))
        if not sym or qty <= 0:
            continue
        out[sym] = {"qty": qty,
                    "avg_price": _num(_dig(r, "averagePrice", "avgPrice", "buyAvgPrice")),
                    "raw": r}
    return out


def cash_available(client) -> float:
    """Free cash for CNC buys.

    ⚠️ CALL limits() UNFILTERED. Verified against Kotak Rohit (UCC 15P56) on
    2026-09-10 with Rs 10,00,000 actually in the account:

        limits(segment="CASH", exchange="NSE", product="CNC") -> every field 0
        limits()                                              -> Net = 1000000

    Kotak returns a well-formed, all-zero response for the filtered call rather
    than an error, so the earlier version reported Rs 0.00 on a funded account and
    looked exactly like a failed deposit. Sizing off that would deploy nothing.
    The filtered call is kept only as a fallback.

    ⚠️ Net INCLUDES MARGIN AGAINST HOLDINGS once they settle. Verified 2026-09-21:
        Net 738,989.36 = Collateral 723,699.86 (haircut value of the 40 settled
        holdings) + CollateralValue 15,289.50 (the actual cash)
    Counting that margin as cash inflated NAV to 17.2L (+72%). Real cash is
    Net - Collateral. On 15/16-Sep Collateral was 0 (holdings unsettled).
    """
    best = 0.0
    for kwargs in ({}, {"segment": "ALL"},
                   {"segment": "CASH", "exchange": "NSE", "product": PRODUCT}):
        try:
            resp = client.limits(**kwargs)
        except Exception:
            continue
        if not isinstance(resp, dict):
            continue
        e = _err(resp)
        if e and not any(t in e.lower() for t in ("no data", "not found", "no record")):
            continue
        v = _num(_dig(resp, "Net", "net", "AvailableCash", "availableCash",
                      "MarginAvailable", "CollateralValue"))
        v -= _num(_dig(resp, "Collateral", "collateral"))   # margin on holdings is not cash
        if v > best:
            best = v
    return best


def account_value(client, marks: dict) -> dict:
    """NAV = holdings marked to market + free cash. Requires a dedicated account."""
    h = holdings(client)
    cash = cash_available(client)
    mv = sum(v["qty"] * float(marks.get(s, v["avg_price"])) for s, v in h.items())
    return {"holdings": h, "cash": round(cash, 2),
            "market_value": round(mv, 2), "nav": round(mv + cash, 2)}


# ── writes ───────────────────────────────────────────────────────────────────

_tag_seq = 0


def unique_tag(prefix: str) -> str:
    """A DIFFERENT tag for every order, all sharing `prefix`.

    Kotak uses the order tag as the order's CLIENT ORDER ID and rejects a repeat.
    DualMom sent the constant tag "dualmom" on every order: the 1-share IDEA test on
    2026-09-15 used it first, and all 40 basket orders that followed were rejected
    with "error from core" - Rs 0 deployed. (The strangle's Kotak mirror hit the
    same wall on 2026-09-07 as "Client OrderID already exists".)
    Day + time + a 3-digit rolling counter = unique for up to 1,000 orders in one
    second. 13 chars with the 2-char prefix - the same length the strangle's tags
    ("vwsk" + HHMMSS + NNN) already run live on Kotak.
    """
    global _tag_seq
    _tag_seq = (_tag_seq + 1) % 1000
    return f"{prefix}{datetime.now():%d%H%M%S}{_tag_seq:03d}"


def place_limit(client, trading_symbol: str, side: str, qty: int, price: float,
                tick: float = 0.05,
                tag: str = "dm") -> dict:
    """Place ONE marketable-limit CNC order. `tag` is a PREFIX - see unique_tag().

    Returns a RESULT DICT, never raises on a broker refusal — the strategy has to
    be able to see its own failure (Live Order Safety rule 2).
    """
    if qty <= 0:
        return {"status": "SKIPPED", "reason": "qty<=0", "order_id": None}
    tag = unique_tag(tag)
    try:
        resp = client.place_order(
            exchange_segment=SEGMENT,
            product=PRODUCT,
            price=str(round_tick(price, tick)),
            order_type="L",
            quantity=str(int(qty)),
            validity="DAY",
            trading_symbol=trading_symbol,
            transaction_type=_txn_code(side),
            tag=tag,
        )
    except Exception as ex:                       # client-side validation errors
        return {"status": "REJECTED", "reason": f"{type(ex).__name__}: {ex}",
                "order_id": None, "margin": False, "raw": None, "tag": tag}

    msg = _err(resp)
    if msg:
        return {"status": "REJECTED", "reason": msg, "order_id": None,
                "margin": is_margin_error(msg), "raw": resp, "tag": tag}
    oid = _extract_oid(resp)
    if not oid:
        return {"status": "UNKNOWN", "reason": "no order id in reply",
                "order_id": None, "margin": False, "raw": resp, "tag": tag}
    return {"status": "PLACED", "order_id": str(oid), "reason": None,
            "margin": False, "raw": resp, "tag": tag}


def order_status(client, order_id: str) -> dict:
    """{status, filled_qty, avg_price}. The ONLY thing allowed to close a leg."""
    resp = client.order_report()
    rows = resp.get("data") if isinstance(resp, dict) else resp
    for r in rows or []:
        if str(_dig(r, "nOrdNo", "orderId", "order_id") or "") == str(order_id):
            raw = str(_dig(r, "ordSt", "status", "orderStatus") or "").lower()
            return {"status": raw or "unknown",
                    "filled_qty": int(_num(_dig(r, "fldQty", "filledQuantity", "filled_qty"))),
                    "avg_price": _num(_dig(r, "avgPrc", "averagePrice", "avg_price")),
                    "rejection": _dig(r, "rejRsn", "rejectionReason"),
                    "raw": r}
    return {"status": "not_found", "filled_qty": 0, "avg_price": 0.0, "raw": None}


def await_fill(client, order_id: str, timeout: int = 120, poll: int = 2) -> dict:
    """Poll to a TERMINAL state. 'Tried' is not 'done' — a position changes state
    in our book only when the broker confirms it."""
    deadline = time.time() + timeout
    last = {"status": "unknown", "filled_qty": 0, "avg_price": 0.0}
    while time.time() < deadline:
        last = order_status(client, order_id)
        s = last["status"]
        if any(k in s for k in ("complete", "filled", "executed")):
            return {**last, "terminal": True, "ok": True}
        if any(k in s for k in ("reject", "cancel")):
            return {**last, "terminal": True, "ok": False}
        time.sleep(poll)
    return {**last, "terminal": False, "ok": False}


def cancel(client, order_id: str) -> dict:
    try:
        return client.cancel_order(order_id=str(order_id))
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"}


def strategy_orders(client, tag: str = "dm") -> list:
    """Our orders only, scoped by tag — never the broker's netted position.

    VERIFIED LIVE 2026-09-15 (order 260915000238827, 1 IDEA on Kotak Rohit): Kotak
    DOES echo the tag, but in order_report it is called `GuiOrdId` (also copied to
    `ordModNo`) - there is no field named "tag". The first go-live test looked only
    for tag/orderTag/usrOrdTag, found nothing and reported the round-trip as failed.
    """
    resp = client.order_report()
    rows = resp.get("data") if isinstance(resp, dict) else resp
    out = []
    for r in rows or []:
        t = str(_dig(r, "GuiOrdId", "tag", "orderTag", "usrOrdTag") or "")
        # every order carries a UNIQUE tag sharing this prefix (see unique_tag)
        if t.startswith(tag) and t[len(tag):].isdigit():
            out.append(r)
    return out
