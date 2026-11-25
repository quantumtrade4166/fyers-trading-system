"""
feedlink/kite_master.py - Kite's public instrument list, matched to contracts by what they ARE.
==============================================================================================

Kite and Kotak number contracts differently. Both describe them the same way, so we match on
(Kotak segment, underlying name, expiry YYYY-MM-DD, strike, kind CE / PE / FUT).

The list is public (https://api.kite.trade/instruments/<EXCHANGE>, no login) and is cached to
feedlink/data/kite_<EXCH>_<date>.csv - one download per exchange per day.

Index spots (Kite tokens are fixed):  NIFTY 50 256265 · NIFTY BANK 260105 · SENSEX 265.
"""
import csv
import datetime as dt
import io
import logging
from pathlib import Path

log = logging.getLogger("feedlink.master")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DATA = Path(__file__).resolve().parent / "data"
URL = "https://api.kite.trade/instruments/{}"

EXCH_SEG = {"NFO": "nse_fo", "BFO": "bse_fo", "MCX": "mcx_fo"}
SEG_EXCH = {v: k for k, v in EXCH_SEG.items()}

# index spot: Kite token <-> Kotak feed key (Kotak streams an index by NAME) and product name
INDEX = {256265: ("nse_cm|Nifty 50", "NIFTY"), 260105: ("nse_cm|Nifty Bank", "BANKNIFTY"),
         265: ("bse_cm|SENSEX", "SENSEX")}
INDEX_BY_KEY = {k.lower(): tok for tok, (k, _) in INDEX.items()}
INDEX_BY_NAME = {n: tok for tok, (_, n) in INDEX.items()}


def _today() -> str:
    return dt.datetime.now(IST).date().isoformat()


def fetch(exchange: str, today: str | None = None) -> str:
    """Today's list of one exchange (CSV text), cached."""
    import requests
    today = today or _today()
    DATA.mkdir(parents=True, exist_ok=True)
    cache = DATA / f"kite_{exchange}_{today}.csv"
    if cache.exists() and cache.stat().st_size > 10_000:
        return cache.read_text(encoding="utf-8")
    text = requests.get(URL.format(exchange), timeout=90).text
    if "instrument_token" not in text[:200]:
        raise RuntimeError(f"Kite {exchange} list: unexpected reply {text[:120]!r}")
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(cache)
    for old in DATA.glob(f"kite_{exchange}_*.csv"):
        if old != cache:
            old.unlink(missing_ok=True)
    return text


def _strike(v) -> float:
    try:
        return round(float(v or 0), 2)
    except ValueError:
        return 0.0


class KiteMaster:
    """(seg, name, expiry, strike, kind) <-> Kite instrument_token, for the given exchanges."""

    def __init__(self, exchanges=("NFO", "BFO"), texts: dict | None = None, names=None):
        self.fwd: dict = {}
        self.back: dict = {}
        names = set(names) if names else None
        for ex in exchanges:
            text = (texts or {}).get(ex)
            if text is None:
                text = fetch(ex)
            seg = EXCH_SEG[ex]
            for r in csv.DictReader(io.StringIO(text)):
                kind = (r.get("instrument_type") or "").upper()
                if kind not in ("CE", "PE", "FUT"):
                    continue
                name = (r.get("name") or "").strip().upper()
                if names and name not in names:
                    continue
                exp = (r.get("expiry") or "").strip()
                if not exp:
                    continue
                k = (seg, name, exp, 0.0 if kind == "FUT" else _strike(r.get("strike")), kind)
                tok = int(r["instrument_token"])
                self.fwd[k] = tok
                self.back[tok] = k
        log.info("Kite list: %d contracts (%s)", len(self.fwd), ",".join(exchanges))

    def token(self, seg: str, name: str, expiry: str, strike: float, kind: str) -> int | None:
        if kind == "IDX":
            return INDEX_BY_NAME.get(name.upper())
        return self.fwd.get((seg, name.upper(), expiry, 0.0 if kind == "FUT" else round(float(strike), 2), kind))

    def info(self, token: int):
        return self.back.get(int(token))
