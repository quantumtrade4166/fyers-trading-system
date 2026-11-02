"""
cepe_imbalance/instruments.py — option + future universe for the 7 underlyings from Kotak's scrip masters.
=========================================================================================================

Kotak encodings, checked on the 2026-11-02 files (samples in testdata/):
  strike `dStrikePrice;` and tick `dTickSize` are x100 (strike may be written "1.7e+06")
  expiry `lExpiryDate` is an epoch; take its UTC date
      nse_fo  counts from 1980-01-01 (add C.EPOCH_1980)   1478183400 -> 2026-11-03
      bse_fo  counts from 1970   (23:59:59 IST)           1793903399 -> 2026-11-05
      mcx_fo  counts from 1970   (23:59:59 UTC)
  instrument types   nse_fo OPTIDX/FUTIDX · bse_fo IO/IF · mcx_fo OPTFUT/FUTCOM (COM = spot row, skipped)
  rupee multiplier   dGenNum/dGenDen x dPriceNum/dPriceDen (GOLD 100, GOLDM 0.1, others 1)

Every contract is keyed "seg|token" — tokens are NOT unique across segments.
"""
import csv
import datetime as dt
import io
import logging
from dataclasses import dataclass, field

from live_trading_options.cepe_imbalance import config as C

log = logging.getLogger("cepe.instruments")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

OPT_TYPES = {"OPTIDX", "IO", "OPTFUT"}
FUT_TYPES = {"FUTIDX", "IF", "FUTCOM"}


@dataclass
class Contract:
    key: str                 # "seg|token"
    seg: str
    token: int
    symbol: str              # pTrdSymbol
    name: str                # underlying name (NIFTY, GOLDM, ...)
    kind: str                # "FUT" | "CE" | "PE"
    expiry: str              # YYYY-MM-DD
    strike: float            # 0 for futures
    lot: int                 # units per lot
    tick: float
    mult: float = 1.0        # rupees per (1 point x 1 unit)

    @property
    def rupees_per_point_lot(self) -> float:
        return self.lot * self.mult


@dataclass
class Underlying:
    name: str
    seg: str
    spot_key: str | None                                         # index spot feed key, None for commodities
    lot: int = 0
    mult: float = 1.0
    futures: list = field(default_factory=list)                  # [Contract] by expiry
    chains: dict = field(default_factory=dict)                   # expiry -> {strike: {"CE": c, "PE": c}}

    def expiries(self) -> list:
        return sorted(self.chains)

    def strikes(self, expiry: str) -> list:
        return sorted(self.chains.get(expiry, {}))

    def future_for(self, expiry: str):
        """Future the options of `expiry` are written on (first future expiring on/after it)."""
        for f in self.futures:
            if f.expiry >= expiry:
                return f
        return self.futures[-1] if self.futures else None


@dataclass
class Universe:
    underlyings: dict                                            # name -> Underlying
    by_key: dict                                                 # "seg|token" -> Contract
    file_date: str = ""


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def expiry_date(seg: str, epoch) -> str | None:
    e = int(_f(epoch, 0))
    if e <= 0:
        return None
    if seg == "nse_fo":
        e += C.EPOCH_1980
    return dt.datetime.fromtimestamp(e, dt.timezone.utc).date().isoformat()


def parse(seg: str, text: str, names, today: str | None = None) -> list:
    """One segment's scrip master -> [Contract] for `names` (exact pSymbolName), live expiries only."""
    today = today or dt.datetime.now(IST).date().isoformat()
    names = set(names)
    out = []
    for raw in csv.DictReader(io.StringIO(text)):
        name = (raw.get("pSymbolName") or "").strip()
        if name not in names:
            continue
        r = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        inst = r.get("pInstType", "")
        if inst in OPT_TYPES:
            kind = r.get("pOptionType", "").upper()
            if kind not in ("CE", "PE"):
                continue
        elif inst in FUT_TYPES:
            kind = "FUT"
        else:
            continue
        expiry = expiry_date(seg, r.get("lExpiryDate"))
        if not expiry or expiry < today:
            continue
        tok = int(r["pSymbol"])
        mult = ((_f(r.get("dGenNum"), 1) or 1) / (_f(r.get("dGenDen"), 1) or 1)
                * (_f(r.get("dPriceNum"), 1) or 1) / (_f(r.get("dPriceDen"), 1) or 1))
        out.append(Contract(key=f"{seg}|{tok}", seg=seg, token=tok, symbol=r.get("pTrdSymbol", ""), name=name,
                            kind=kind, expiry=expiry,
                            strike=0.0 if kind == "FUT" else round(_f(r.get("dStrikePrice;", r.get("dStrikePrice"))) / 100, 2),
                            lot=int(_f(r.get("lLotSize"), 1)) or 1, tick=_f(r.get("dTickSize")) / 100, mult=mult))
    return out


def build(contracts_by_seg: dict, today: str | None = None) -> Universe:
    """{seg: csv text} -> Universe for every name in C.UNDERLYINGS."""
    unds, by_key = {}, {}
    for name, spec in C.UNDERLYINGS.items():
        unds[name] = Underlying(name=name, seg=spec["seg"], spot_key=spec["spot"])
    for seg, text in contracts_by_seg.items():
        names = [n for n, s in C.UNDERLYINGS.items() if s["seg"] == seg]
        for c in parse(seg, text, [C.UNDERLYINGS[n]["scrip"] for n in names], today=today):
            u = next(unds[n] for n in names if C.UNDERLYINGS[n]["scrip"] == c.name)
            by_key[c.key] = c
            if c.kind == "FUT":
                u.futures.append(c)
            else:
                u.chains.setdefault(c.expiry, {}).setdefault(c.strike, {})[c.kind] = c
    for u in unds.values():
        u.futures.sort(key=lambda c: c.expiry)
        for e in list(u.chains):
            u.chains[e] = {k: v for k, v in sorted(u.chains[e].items()) if "CE" in v and "PE" in v}
            if not u.chains[e]:
                del u.chains[e]
        if u.chains:
            first = next(iter(u.chains[u.expiries()[0]].values()))["CE"]
            u.lot, u.mult = first.lot, first.mult
    return Universe(underlyings=unds, by_key=by_key, file_date=today or "")


def load(client) -> Universe:
    """Today's three scrip masters (cached to data/scrip_{seg}_{date}.csv, one download a day)."""
    import requests
    today = dt.datetime.now(IST).date().isoformat()
    C.DATA.mkdir(parents=True, exist_ok=True)
    texts = {}
    for seg in sorted({s["seg"] for s in C.UNDERLYINGS.values()}):
        cache = C.DATA / f"scrip_{seg}_{today}.csv"
        if cache.exists() and cache.stat().st_size > 100_000:
            texts[seg] = cache.read_text(encoding="utf-8")
            continue
        url = client.scrip_master(exchange_segment=seg)
        if not isinstance(url, str) or not url.startswith("http"):
            raise RuntimeError(f"{seg}: scrip_master gave no URL: {str(url)[:200]}")
        texts[seg] = requests.get(url, timeout=90).text
        cache.write_text(texts[seg], encoding="utf-8")
        for old in C.DATA.glob(f"scrip_{seg}_*.csv"):
            if old != cache:
                old.unlink(missing_ok=True)
    u = build(texts, today=today)
    u.file_date = today
    for x in u.underlyings.values():
        log.info("%s: lot %s mult %s, %d futures, %d option expiries (nearest %s)", x.name, x.lot, x.mult,
                 len(x.futures), len(x.chains), (x.expiries() or ["-"])[0])
    return u
