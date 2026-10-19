"""
mcx_terminal/instruments.py — MCX contract universe from Kotak's scrip master (mcx_fo).
=====================================================================================

Kotak's encodings (checked on the 2026-10-19 file):
  - strike (`dStrikePrice;`) and tick size (`dTickSize`) are x100      930000 -> 9300.00, 10 -> 0.10
  - expiry (`lExpiryDate`) = epoch of 23:59:59 UTC on the expiry date  -> take its UTC date
  - pInstType: OPTFUT = option, FUTCOM = future, COM = spot row (no expiry, not tradable here)
  - pSymbolName is exact: CRUDEOIL and CRUDEOILM (mini) are different products

Option underlying = the first future of the same product expiring on/after the option.
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


@dataclass
class Product:
    name: str
    lot: int = 0
    opt_tick: float = 0.0
    futures: list = field(default_factory=list)                  # [Contract] sorted by expiry
    chains: dict = field(default_factory=dict)                   # expiry -> {strike: {"CE": Contract, "PE": Contract}}

    def expiries(self) -> list:
        return sorted(self.chains)

    def underlying(self, expiry: str):
        """Future the options of `expiry` are written on (first future expiring on/after it)."""
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


def parse(text: str, names=C.INSTRUMENTS, today: str | None = None) -> Universe:
    today = today or dt.datetime.now(IST).date().isoformat()
    rows = csv.DictReader(io.StringIO(text))
    products = {n: Product(n) for n in names}
    by_token = {}
    for raw in rows:
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        name = r.get("pSymbolName", "")
        if name not in products:
            continue
        inst = r.get("pInstType", "")
        if inst not in ("OPTFUT", "FUTCOM"):
            continue
        e = int(_f(r.get("lExpiryDate"), 0))
        if e <= 0:
            continue
        expiry = dt.datetime.fromtimestamp(e, dt.timezone.utc).date().isoformat()
        if expiry < today:
            continue
        opt = r.get("pOptionType", "").upper()
        kind = "FUT" if inst == "FUTCOM" else opt
        if kind not in ("FUT", "CE", "PE"):
            continue
        c = Contract(token=int(r["pSymbol"]), symbol=r.get("pTrdSymbol", ""), name=name, kind=kind,
                     expiry=expiry, strike=round(_f(r.get("dStrikePrice;", r.get("dStrikePrice"))) / 100, 2),
                     lot=int(_f(r.get("lLotSize"), 1)), tick=_f(r.get("dTickSize")) / 100,
                     freeze=int(_f(r.get("lFreezeQty"), 0)),
                     mult=(_f(r.get("dGenNum"), 1) or 1) / (_f(r.get("dGenDen"), 1) or 1)
                     * (_f(r.get("dPriceNum"), 1) or 1) / (_f(r.get("dPriceDen"), 1) or 1))
        p = products[name]
        p.lot = p.lot or c.lot
        by_token[c.token] = c
        if kind == "FUT":
            p.futures.append(c)
        else:
            p.opt_tick = p.opt_tick or c.tick
            p.chains.setdefault(expiry, {}).setdefault(c.strike, {})[kind] = c
    for p in products.values():
        p.futures.sort(key=lambda c: c.expiry)
        # keep only strikes that have both legs listed
        for e in list(p.chains):
            p.chains[e] = {k: v for k, v in sorted(p.chains[e].items()) if "CE" in v and "PE" in v}
            if not p.chains[e]:
                del p.chains[e]
    return Universe(products=products, by_token=by_token)


def load(client) -> Universe:
    """Today's scrip master, cached to data/scrip_mcx_YYYY-MM-DD.csv (one download a day)."""
    import requests
    today = dt.datetime.now(IST).date().isoformat()
    cache = C.DATA / f"scrip_mcx_{today}.csv"
    if cache.exists() and cache.stat().st_size > 100_000:
        text = cache.read_text(encoding="utf-8")
    else:
        url = client.scrip_master(exchange_segment="mcx_fo")
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError(f"scrip_master gave no URL: {str(url)[:200]}")
        text = requests.get(url, timeout=60).text
        cache.write_text(text, encoding="utf-8")
        for old in C.DATA.glob("scrip_mcx_*.csv"):
            if old != cache:
                old.unlink(missing_ok=True)
    u = parse(text, today=today)
    u.file_date = today
    for p in u.products.values():
        log.info("%s: lot %s, %d futures, %d option expiries", p.name, p.lot, len(p.futures), len(p.chains))
    return u
