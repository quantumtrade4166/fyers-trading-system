"""
vwap/strategy.py — the pure parts of the BTC VWAP strangle.
===========================================================

The NSE Vwap Strangle (live_trading_options/strangle_strategy) ported to Delta
Exchange BTC daily options. Nothing in this file does I/O or reads a clock, so
every rule below is exactly testable offline (vwap/test_vwap.py).

    Session       when a cycle starts, stops taking entries, and flattens
    select_pair   the strikes: first same-distance CE+PE with combined <= threshold
    CandleBuilder 5-min combined-premium candles + cumulative typical-price VWAP
    Trigger       the entry/exit state machine — a line-for-line port of
                  strangle_strategy/live/trigger_engine.py, on datetimes instead of
                  "HH:MM" because the full-expiry cycle crosses midnight

THE RULES (unchanged from NSE)
  ENTRY  a red candle closing below VWAP arms a sell at low - 1. It FILLS the
         instant the combined premium trades down to it. The next candle close
         cancels it (close above VWAP), replaces it (fresh red-below-VWAP, new
         low - 1), or cancels it (anything else).
  EXIT   any candle closing above VWAP -> buy both legs back at market.
  One position at a time, strict entry -> exit alternation, max N fills a cycle,
  no new entry after the cutoff, flatten at square-off.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


# ── session clock ────────────────────────────────────────────────────────
class Session:
    """One version's clock. `start` and `square_off` are IST wall-clock times; a
    square-off at or before the start means the cycle ends the NEXT day."""

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.label = cfg.get("label", name)
        self.start = _t(cfg["start"])
        self.cutoff = _t(cfg["entry_cutoff"])
        self.square_off = _t(cfg["square_off"])
        self.threshold = float(cfg["combined_threshold"])
        self.contracts = int(cfg["contracts"])
        self.max_entries = int(cfg["max_entries"])
        self.mtm_stop = float(cfg.get("mtm_stop_usd") or 0)
        self.fallback_last = bool(cfg.get("fallback_last_pair", False))

    @property
    def spans_midnight(self) -> bool:
        return self.square_off <= self.start

    def bounds(self, now: dt.datetime):
        """(start, end) of the cycle `now` is inside, else None (the flat gap)."""
        d = now.date()
        if not self.spans_midnight:
            s = dt.datetime.combine(d, self.start)
            e = dt.datetime.combine(d, self.square_off)
            return (s, e) if s <= now < e else None
        if now.time() >= self.start:
            return (dt.datetime.combine(d, self.start),
                    dt.datetime.combine(d + dt.timedelta(days=1), self.square_off))
        if now.time() < self.square_off:
            return (dt.datetime.combine(d - dt.timedelta(days=1), self.start),
                    dt.datetime.combine(d, self.square_off))
        return None

    def cutoff_dt(self, bounds) -> dt.datetime:
        """The entry cutoff inside this cycle. It is on the END date unless the
        cutoff time falls after the start time on the start date."""
        s, e = bounds
        c = dt.datetime.combine(s.date(), self.cutoff)
        if s < c <= e:
            return c
        return dt.datetime.combine(e.date(), self.cutoff)

    @staticmethod
    def key(bounds) -> str:
        return f"{bounds[0]:%Y-%m-%dT%H:%M}"

    @staticmethod
    def expiry_date(bounds) -> dt.date:
        """Both versions sell the contract that settles at 17:30 on the day the
        cycle ends: ist_day today's, full_expiry tomorrow's."""
        return bounds[1].date()


# ── strike selection ─────────────────────────────────────────────────────
def select_pair(marks: dict, strikes: list, spot: float, threshold: float,
                fallback_last: bool = False):
    """The first CE+PE pair, walking OUTWARD from ATM, whose combined mark is at or
    below `threshold`.

    `marks` is {(strike, "CE"|"PE"): mark}. Same rule as NSE: both legs EXACTLY the
    same distance from ATM (ATM+d CE with ATM-d PE), first level under the
    threshold. Delta's grid is non-uniform and not always listed as far on the put
    side as the call side, so only distances where BOTH strikes are actually listed
    count as levels. ATM itself is never sold.

    `fallback_last`: if no level is under the threshold, take the LAST (farthest)
    equidistant level instead — the cheapest the listed grid allows. Flagged with
    `over_threshold: True`. Used by full_expiry, whose next-day premium can sit
    above the threshold at the far edge of the listed grid.

    Returns a dict (ce, pe, ce_mark, pe_mark, combined, distance, atm,
    over_threshold) or None.
    """
    strikes = sorted(set(float(s) for s in strikes))
    if not strikes or spot is None:
        return None
    atm = min(strikes, key=lambda s: (abs(s - spot), s))
    listed = set(strikes)
    last = None
    for ce in (s for s in strikes if s > atm):
        d = ce - atm
        pe = atm - d
        if pe not in listed:
            continue                      # no equidistant put listed at this level
        cm, pm = marks.get((ce, "CE")), marks.get((pe, "PE"))
        if cm is None or pm is None:
            continue
        pick = {"ce": ce, "pe": pe, "ce_mark": round(cm, 4), "pe_mark": round(pm, 4),
                "combined": round(cm + pm, 4), "distance": d, "atm": atm,
                "over_threshold": False}
        if cm + pm <= threshold:
            return pick
        last = pick
    if fallback_last and last is not None:
        return dict(last, over_threshold=True)
    return None


# ── candles + VWAP ───────────────────────────────────────────────────────
def bucket_start(now: dt.datetime, minutes: int = 5) -> dt.datetime:
    return now.replace(minute=(now.minute // minutes) * minutes, second=0, microsecond=0)


class CandleBuilder:
    """5-minute combined-premium candles from synchronized samples.

    Each sample is CE + PE taken at the same instant, so the candle's high and low
    are highs and lows the PAIR actually traded at — not the sum of each leg's own
    high and low, which happen at different moments and inflate the range (the
    iCharts lesson, see [[Combined Premium VWAP Formula]]).

    Samples before `first_bucket` are ignored: a cycle that starts late begins on
    the next clean 5-minute boundary rather than with a partial first candle.
    """

    def __init__(self, first_bucket: dt.datetime, minutes: int = 5):
        self.minutes = minutes
        self.first = first_bucket
        self.cur = None              # forming candle
        self.closed: list = []       # finished candles, each with volume + vwap
        self.cum_pv = 0.0
        self.cum_v = 0.0
        self.cum_typ = 0.0
        self.n = 0

    def add(self, now: dt.datetime, value: float):
        """Absorb a sample. Returns the candle that CLOSED because of it, if any
        (the caller then attaches volume via `finalize`)."""
        if value is None or now < self.first:
            return None
        b = bucket_start(now, self.minutes)
        m = now.replace(second=0, microsecond=0).isoformat()
        out = None
        if self.cur is not None and b > self.cur["start"]:
            out = self._close()
        # SMOOTHED WICKS — the NIFTY premium_builder Method A (matches iCharts): the
        # 5-min high/low is the max/min of each MINUTE's synchronized OPEN and CLOSE
        # points only. Prices in the middle of a minute never make a wick.
        if self.cur is None:
            self.cur = {"start": b, "open": value, "high": value, "low": value,
                        "close": value, "samples": 0,
                        "m": m, "hi_fixed": value, "lo_fixed": value}
        c = self.cur
        if c["m"] != m:
            # the previous minute's close is now final, and this sample is the new
            # minute's open — both become fixed wick points
            c["hi_fixed"] = max(c["hi_fixed"], c["close"], value)
            c["lo_fixed"] = min(c["lo_fixed"], c["close"], value)
            c["m"] = m
        c["close"] = value           # this minute's close so far (provisional)
        c["high"] = max(c["hi_fixed"], value)
        c["low"] = min(c["lo_fixed"], value)
        c["samples"] += 1
        return out

    def due(self, now: dt.datetime):
        """Close the forming candle on the clock, even if no new sample has come —
        a feed pause must not hold a signal hostage. Returns the closed candle."""
        if self.cur is not None and now >= self.cur["start"] + dt.timedelta(minutes=self.minutes):
            return self._close()
        return None

    def _close(self):
        c, self.cur = self.cur, None
        return c

    def finalize(self, c: dict, volume: float) -> dict:
        """Attach the traded volume and the cumulative VWAP through this candle.

        VWAP = sum(typical x volume) / sum(volume), typical = (H+L+C)/3 — the
        formula validated against iCharts on NSE. BTC option volume is lumpy, so
        until the first contract trades in the cycle VWAP falls back to the
        running mean of typical prices rather than being undefined."""
        typ = (c["high"] + c["low"] + c["close"]) / 3.0
        v = max(0.0, float(volume or 0))
        self.cum_pv += typ * v
        self.cum_v += v
        self.cum_typ += typ
        self.n += 1
        vwap = (self.cum_pv / self.cum_v) if self.cum_v > 0 else (self.cum_typ / self.n)
        c = dict(c, volume=v, typical=round(typ, 4), vwap=round(vwap, 4),
                 vwap_src="volume" if self.cum_v > 0 else "twap")
        for k in ("open", "high", "low", "close"):
            c[k] = round(c[k], 4)
        self.closed.append(c)
        return c

    def live_vwap(self):
        """VWAP including the forming candle at zero volume weight — for display."""
        return self.closed[-1]["vwap"] if self.closed else None

    # persistence
    def to_dict(self) -> dict:
        ser = lambda c: dict(c, start=c["start"].isoformat())
        return {"first": self.first.isoformat(), "minutes": self.minutes,
                "closed": [ser(c) for c in self.closed],
                "cur": ser(self.cur) if self.cur else None,
                "cum_pv": self.cum_pv, "cum_v": self.cum_v,
                "cum_typ": self.cum_typ, "n": self.n}

    @classmethod
    def from_dict(cls, d: dict) -> "CandleBuilder":
        de = lambda c: dict(c, start=dt.datetime.fromisoformat(c["start"]))
        b = cls(dt.datetime.fromisoformat(d["first"]), int(d.get("minutes", 5)))
        b.closed = [de(c) for c in d.get("closed") or []]
        b.cur = de(d["cur"]) if d.get("cur") else None
        b.cum_pv, b.cum_v = float(d["cum_pv"]), float(d["cum_v"])
        b.cum_typ, b.n = float(d["cum_typ"]), int(d["n"])
        return b


# ── the entry / exit state machine ───────────────────────────────────────
class Trigger:
    """Port of strangle_strategy/live/trigger_engine.LiveTrigger.

    Emits intent through two callbacks — on_entry(trigger, n, reason) and
    on_exit(n, reason) — and never prices anything itself; the book fills against
    the real order book. A callback that reports failure (returns False) leaves the
    state as it was BEFORE the attempt was committed, never re-firing: the NSE
    "orders never fired" bug was a trigger that re-armed on every tick after a
    failed order.
    """

    def __init__(self, on_entry, on_exit, *, max_entries: int, cutoff: dt.datetime,
                 offset: float = 1.0, minutes: int = 5):
        self.on_entry, self.on_exit = on_entry, on_exit
        self.max_entries = max_entries
        self.cutoff = cutoff
        self.offset = offset
        self.minutes = minutes
        self.entries = 0
        self.in_pos = False
        self.pending = None          # {"trigger": float, "signal": iso}
        self.done = False
        self.entered_bucket = None   # iso start of the candle the fill landed in

    def on_tick(self, premium: float, now: dt.datetime):
        if self.done or self.in_pos or self.pending is None or premium is None:
            return
        if now > self.cutoff:
            self.pending = None
            return
        if premium <= self.pending["trigger"]:
            armed = self.pending
            trig, sig = armed["trigger"], armed["signal"]
            self.entries += 1
            self.in_pos = True
            self.pending = None
            self.entered_bucket = bucket_start(now, self.minutes).isoformat()
            ok = self.on_entry(trig, self.entries, f"trigger {trig} (signal {sig}) hit")
            if ok == "retry":
                # The book could not fill this near the trigger (thin levels). Nothing
                # was sold, so the signal is still valid: put the trigger back and let
                # a later tick try again — it does NOT burn one of the day's entries.
                self.entries -= 1
                self.in_pos = False
                self.entered_bucket = None
                self.pending = armed
            elif ok is False:
                # the fill was refused and nothing is held: stay flat, count the
                # attempt, and wait for a fresh signal — never re-fire this one
                self.in_pos = False

    def on_candle_close(self, c: dict):
        if self.done:
            return
        start = c["start"]
        o, cl, low, vwap = c["open"], c["close"], c["low"], c["vwap"]
        red, below, above = cl < o, cl < vwap, cl > vwap
        can_arm = self.entries < self.max_entries and start + dt.timedelta(
            minutes=self.minutes) <= self.cutoff

        if self.in_pos:
            if self.entered_bucket == start.isoformat():
                return                      # entered inside this candle: don't exit it too
            if above:
                if self.on_exit(self.entries, "close above VWAP") is not False:
                    self.in_pos = False
            return

        if self.pending is not None:
            if red and below and can_arm:
                self.pending = {"trigger": round(low - self.offset, 4),
                                "signal": start.isoformat()}      # replace
            else:
                self.pending = None                               # cancel
            return

        if can_arm and red and below:
            self.pending = {"trigger": round(low - self.offset, 4),
                            "signal": start.isoformat()}

    def to_dict(self) -> dict:
        return {"entries": self.entries, "in_pos": self.in_pos, "pending": self.pending,
                "done": self.done, "entered_bucket": self.entered_bucket}

    def load(self, d: dict):
        self.entries = int(d.get("entries", 0))
        self.in_pos = bool(d.get("in_pos"))
        self.pending = d.get("pending")
        self.done = bool(d.get("done"))
        self.entered_bucket = d.get("entered_bucket")
