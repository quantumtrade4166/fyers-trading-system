"""
mcx_terminal/settings.py — order-placement settings (engine-side, saved to data/settings.json).
=============================================================================================

Kotak takes no MARKET orders through the API, so every order is a LIMIT. These settings decide
where an "at the market" limit price comes from and how a waiting order is chased:

  entry_ref / exit_ref   "touch" = best ask (buy) / best bid (sell) · "ltp" = last traded price
  *_buffer, *_unit       added to the reference so the limit fills easier: BUY at ref + buffer,
                         SELL at ref - buffer; unit "%" or "pts"; rounded to the tick (never worse)
  reprice                a system-priced order still open is re-priced to the current reference
                         every second (user-typed limits are never moved by this)
  market_after_s         any order still open after this many seconds is pushed through the book
                         as an aggressive limit (best opposite price +/- 1 %, fills the rest);
                         0 = off. Applies to typed limits too.
  product / session      MIS | NRML · AUTO | REGULAR | AMO (recorded on paper orders; used live)
  strike_gap             show every Nth strike in the Straddle / Strangle tables, per product
"""
import json
import logging
import threading

from live_trading_options.mcx_terminal import config as C

log = logging.getLogger("mcx.settings")

DEFAULTS = {
    "entry_ref": "touch", "entry_buffer": 1.0, "entry_unit": "%",
    "exit_ref": "touch", "exit_buffer": 1.0, "exit_unit": "%",
    "reprice": True, "market_after_s": 0.0,
    "product": "NRML", "session": "AUTO",
    "strike_gap": {"CRUDEOIL": 1, "GOLDM": 1, "SILVERM": 1},
}
CHOICES = {"entry_ref": ("touch", "ltp"), "exit_ref": ("touch", "ltp"), "entry_unit": ("%", "pts"),
           "exit_unit": ("%", "pts"), "product": ("MIS", "NRML"), "session": ("AUTO", "REGULAR", "AMO")}


class Settings:
    def __init__(self, path=None):
        self.path = path or (C.DATA / "settings.json")
        self.lock = threading.Lock()
        self.v = json.loads(json.dumps(DEFAULTS))
        try:
            if self.path.exists():
                self._merge(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            log.exception("settings load failed; using defaults")

    def get(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.v))

    def _merge(self, new: dict):
        for k, val in new.items():
            if k not in DEFAULTS:
                continue
            if k in CHOICES:
                if val in CHOICES[k]:
                    self.v[k] = val
            elif k in ("entry_buffer", "exit_buffer", "market_after_s"):
                f = float(val or 0)
                if f < 0 or (k.endswith("buffer") and f > 20 and self.v.get(k.replace("buffer", "unit")) == "%"):
                    raise ValueError(f"{k} out of range")
                self.v[k] = f
            elif k == "reprice":
                self.v[k] = bool(val)
            elif k == "strike_gap" and isinstance(val, dict):
                for n, g in val.items():
                    if n in self.v["strike_gap"] and int(g) in (1, 2, 3, 4, 5, 10):
                        self.v["strike_gap"][n] = int(g)

    def update(self, new: dict) -> dict:
        try:
            with self.lock:
                self._merge(new)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.v, indent=1), encoding="utf-8")
                tmp.replace(self.path)
        except (ValueError, TypeError) as e:
            return {"ok": False, "msg": f"settings not saved: {e}"}
        log.info("settings updated: %s", new)
        return {"ok": True, "msg": "settings saved"}
