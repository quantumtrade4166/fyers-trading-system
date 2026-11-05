"""
mcx_terminal/instruments.py — the desk's contract universe from Kotak's scrip masters.
====================================================================================

MCX desk: mcx_fo.   NSE desk: nse_fo (NIFTY, BANKNIFTY) + bse_fo (SENSEX) + the index spot rows.

Kotak's encodings (checked on the files of 2026-10-19 (MCX) and 2026-11-04 (NSE/BSE)):
  - strike (`dStrikePrice;`) and tick size (`dTickSize`) are x100      930000 -> 9300.00, 10 -> 0.10
    EXCEPT nse_com (NSE commodities) strikes: x10000  1484000000 -> 148400 (symbol GOLD26NOV148400CE)
  - NSE commodity GOLD / GOLDM are products NSEGOLD / NSEGOLDM here (config.SYMBOL): MCX has GOLD / GOLDM
  - expiry (`lExpiryDate`) differs per segment:
      mcx_fo  unix epoch of 23:59:59 UTC on the expiry date   -> its UTC date
      nse_fo, nse_com  seconds since 1980-01-01              -> that date
      bse_fo  unix epoch of 23:59:59 IST on the expiry date   -> its IST date
  - pInstType: option OPTFUT (MCX) / OPTIDX (NSE) / IO (BSE); future FUTCOM / FUTIDX / IF
  - pSymbolName is exact: CRUDEOIL and CRUDEOILM (mini) are different products

Option underlying: MCX = the first future of the same product expiring on/after the option;
NSE/BSE index options = the index spot (config.SPOT), streamed from nse_cm / bse_cm.
"""
import csv
import datetime as dt
import io
import logging
from dataclasses import dataclass, field

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.instruments")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@dataclass
class Contract:
    token: int
    symbol: str              # pTrdSymbol, e.g. CRUDEOIL17NOV269300CE
    name: str                # CRUDEOIL
    kind: str                # "FUT" | "CE" | "PE"
    expiry: str              # YYYY-MM-DD
    strike: float            # 0 for futures
    lot: int
    tick: float
    freeze: int = 0          # max units per order at the exchange (lFreezeQty) -> bigger orders are split
    mult: float = 1.0        # rupees per (1 point x 1 unit): GOLDM is quoted per 10 g on 1 g units -> 0.1
    seg: str = "mcx_fo"      # Kotak exchange segment (orders, quotes and the price feed)


@dataclass
class Product:
    name: str
    lot: int = 0
    opt_tick: float = 0.0
    futures: list = field(default_factory=list)                  # [Contract] sorted by expiry
    chains: dict = field(default_factory=dict)                   # expiry -> {strike: {"CE": Contract, "PE": Contract}}
    spot: "Contract | None" = None                               # index spot (NSE desk), kind "IDX", not tradable

    def expiries(self) -> list:
        return sorted(self.chains)

    def underlying(self, expiry: str):
        """What the options of `expiry` are priced off: the index spot (NSE desk), else the first future
        expiring on/after it (MCX)."""
        if self.spot is not None:
            return self.spot
        for f in self.futures:
            if f.expiry >= expiry:
                return f
        return self.futures[-1] if self.futures else None


@dataclass
class Universe:
    products: dict                                               # name -> Product
    by_token: dict                                               # token -> Contract
    file_date: str = ""


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


OPT_TYPES = ("OPTFUT", "OPTIDX", "IO")
FUT_TYPES = ("FUTCOM", "FUTIDX", "IF", "FUTBLN")
STRIKE_DIV = {"nse_com": 10000}            # default 100
_E1980 = dt.datetime(1980, 1, 1, tzinfo=dt.timezone.utc)


def expiry_date(seg: str, e: int) -> str:
    """Kotak's lExpiryDate -> YYYY-MM-DD (each segment counts differently, see module doc)."""
    if seg in ("nse_fo", "nse_com"):
        return (_E1980 + dt.timedelta(seconds=e)).date().isoformat()
    if seg == "bse_fo":
        return dt.datetime.fromtimestamp(e, IST).date().isoformat()
    return dt.datetime.fromtimestamp(e, dt.timezone.utc).date().isoformat()


def parse(text: str, names=None, today: str | None = None, seg: str = "mcx_fo") -> Universe:
    """One scrip-master file -> Universe (MCX desk, tests)."""
    return parse_many({seg: text}, names=names, today=today)


def parse_many(texts: dict, names=None, today: str | None = None, spot: dict | None = None) -> Universe:
    """{segment: scrip-master text} -> Universe. A product is read only from the segment config.SEGMENT
    gives it (rows of it in another file are ignored). spot: {name: (seg, token, label)} index rows."""
    names = tuple(names or C.INSTRUMENTS)
    today = today or dt.datetime.now(IST).date().isoformat()
    spot = C.SPOT if spot is None else spot
    products = {n: Product(n) for n in names}
    by_token = {}
    for seg, text in texts.items():
        # this file's products: Kotak symbol name -> our product key (NSE "GOLD" -> NSEGOLD)
        mine = {getattr(C, "SYMBOL", {}).get(k, k): k for k in names if C.SEGMENT.get(k, seg) == seg}
        sdiv = STRIKE_DIV.get(seg, 100)
        for raw in csv.DictReader(io.StringIO(text)):
            r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
            name = mine.get(r.get("pSymbolName", ""))
            if name is None:
                continue
            inst = r.get("pInstType", "")
            if inst not in OPT_TYPES + FUT_TYPES:
                continue
            e = int(_f(r.get("lExpiryDate"), 0))
            if e <= 0:
                continue
            expiry = expiry_date(seg, e)
            if expiry < today:
                continue
            opt = r.get("pOptionType", "").upper()
            kind = "FUT" if inst in FUT_TYPES else opt
            if kind not in ("FUT", "CE", "PE"):
                continue
            c = Contract(token=int(r["pSymbol"]), symbol=r.get("pTrdSymbol", ""), name=name, kind=kind,
                         expiry=expiry, strike=round(_f(r.get("dStrikePrice;", r.get("dStrikePrice"))) / sdiv, 2),
                         lot=int(_f(r.get("lLotSize"), 1)), tick=_f(r.get("dTickSize")) / 100,
                         freeze=int(_f(r.get("lFreezeQty"), 0)),
                         mult=(_f(r.get("dGenNum"), 1) or 1) / (_f(r.get("dGenDen"), 1) or 1)
                         * (_f(r.get("dPriceNum"), 1) or 1) / (_f(r.get("dPriceDen"), 1) or 1),
                         seg=seg)
            if c.token in by_token and by_token[c.token].seg != seg:
                log.error("token %s is in %s AND %s - %s skipped", c.token, by_token[c.token].seg, seg, c.symbol)
                continue
            p = products[name]
            p.lot = p.lot or c.lot
            by_token[c.token] = c
            if kind == "FUT":
                p.futures.append(c)
            else:
                p.opt_tick = p.opt_tick or c.tick
                p.chains.setdefault(expiry, {}).setdefault(c.strike, {})[kind] = c
    for name, sp in (spot or {}).items():
        sseg, tok, label = sp[:3]
        if name not in products:
            continue
        if tok in by_token:
            log.error("spot token %s of %s collides with %s - no spot", tok, name, by_token[tok].symbol)
            continue
        p = products[name]
        p.spot = Contract(token=int(tok), symbol=label, name=name, kind="IDX", expiry="", strike=0.0,
                          lot=p.lot or 1, tick=0.05, seg=sseg)
        by_token[p.spot.token] = p.spot
    for p in products.values():
        p.futures.sort(key=lambda c: c.expiry)
        # keep only strikes that have both legs listed
        for e in list(p.chains):
            p.chains[e] = {k: v for k, v in sorted(p.chains[e].items()) if "CE" in v and "PE" in v}
            if not p.chains[e]:
                del p.chains[e]
    return Universe(products=products, by_token=by_token)


def _scrip_url(client, seg: str) -> str:
    """Download link of one segment's scrip master (the SDK refuses some segment names -> full list)."""
    try:
        url = client.scrip_master(exchange_segment=seg)
        if isinstance(url, str) and url.startswith("http"):
            return url
    except Exception:
        pass
    allp = client.scrip_master()
    flat = []
    for v in (allp.values() if isinstance(allp, dict) else []):
        flat += v if isinstance(v, list) else [v]
    for u in flat:
        if isinstance(u, str) and u.rsplit("/", 1)[-1].lower().split("-")[0].split(".")[0] == seg:
            return u
    raise RuntimeError(f"scrip_master gave no URL for {seg}: {str(allp)[:200]}")


def load(client) -> Universe:
    """Today's scrip masters, cached to data/scrip_<seg>_YYYY-MM-DD.csv (one download a day each)."""
    import requests
    today = dt.datetime.now(IST).date().isoformat()
    texts = {}
    main = C.SEGMENT[C.INSTRUMENTS[0]]                     # the desk's own exchange: without it, no start
    for seg in sorted(set(C.SEGMENT.values())):
        tag = "mcx" if seg == "mcx_fo" else seg            # MCX keeps its old cache name
        cache = C.DATA / f"scrip_{tag}_{today}.csv"
        if cache.exists() and cache.stat().st_size > 100_000:
            texts[seg] = cache.read_text(encoding="utf-8")
            continue
        try:
            texts[seg] = requests.get(_scrip_url(client, seg), timeout=90).text
        except Exception as e:
            if seg == main:
                raise
            log.error("contract list %s not loaded (%s) - its products are hidden today", seg, e)
            continue
        cache.write_text(texts[seg], encoding="utf-8")
        for old in C.DATA.glob(f"scrip_{tag}_*.csv"):
            if old != cache:
                old.unlink(missing_ok=True)
    u = parse_many(texts, today=today)
    u.file_date = today
    for p in u.products.values():
        log.info("%s: lot %s, %d futures, %d option expiries%s", p.name, p.lot, len(p.futures), len(p.chains),
                 f", spot {p.spot.seg}|{p.spot.token}" if p.spot else "")
    return u
