"""
mcx_terminal/oi.py — open-interest baselines for the chain's OI columns + change meters.
========================================================================================

Kotak's feed carries each contract's LIVE OI but not yesterday's (checked 12-Nov-2026: no field of the
Market Picture packet and no key of the REST quote is a previous OI). So the engine keeps its own:

  data/oi_YYYY-MM-DD.json  {token: [first_oi, last_oi, "HH:MM" first seen]}   (written every SAMPLE_S)

Change "Today" = live OI - baseline, baseline =
  1. the LAST OI seen on the previous recorded day (= yesterday's close), else
  2. the FIRST OI seen today. Before the open that IS yesterday's close (OI moves only with trades);
     a contract first seen after the open is marked `since HH:MM` so the screen can say so.
Change "15 min" = live OI - the OI sampled ~15 minutes ago (in memory; a restart starts it afresh).

Quantities are what the feed sends (MCX: lots, NSE/BSE: units); the screen divides by the chain's oi_div.
Hot path untouched: the housekeeping thread samples a copy of the engine's last ticks once a minute.
"""
import collections
import datetime as dt
import json
import logging
import time

log = logging.getLogger("mcx.oi")
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SAMPLE_S = 60
WINDOW_S = 15 * 60
KEEP_S = WINDOW_S + 3 * SAMPLE_S


class OiBook:
    def __init__(self, folder, open_hhmm: str = "09:00", day: str | None = None):
        self.folder = folder
        self.open_hhmm = open_hhmm
        self.day = day or dt.datetime.now(IST).date().isoformat()
        self.path = folder / f"oi_{self.day}.json"
        self.first: dict[int, tuple] = {}             # tok -> (oi, "HH:MM")
        self.last: dict[int, int] = {}
        self.prev: dict[int, int] = {}                 # tok -> last OI of the previous recorded day
        self.hist: dict[int, collections.deque] = {}   # tok -> deque[(epoch, oi)]
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                for k, (f, l, hm) in d.items():
                    self.first[int(k)] = (f, hm)
                    self.last[int(k)] = l
            prev = sorted(p for p in self.folder.glob("oi_*.json") if p.name < self.path.name)
            if prev:
                d = json.loads(prev[-1].read_text(encoding="utf-8"))
                self.prev = {int(k): v[1] for k, v in d.items()}
            log.info("OI baselines: %d today, %d from %s", len(self.first), len(self.prev),
                     prev[-1].name if prev else "no earlier day")
        except Exception:
            log.exception("OI baselines load failed; starting empty")

    def sample(self, oi_now: dict, now: float | None = None):
        """oi_now = {token: live OI} (only contracts whose OI is known)."""
        now = now or time.time()
        hm = dt.datetime.fromtimestamp(now, IST).strftime("%H:%M")
        for tok, oi in oi_now.items():
            if oi is None:
                continue
            if tok not in self.first:
                self.first[tok] = (oi, hm)
            self.last[tok] = oi
            h = self.hist.get(tok)
            if h is None:
                h = self.hist[tok] = collections.deque()
            h.append((now, oi))
            while h and now - h[0][0] > KEEP_S:
                h.popleft()
        self._save()

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({str(k): [f, self.last.get(k, f), hm] for k, (f, hm) in self.first.items()}),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("OI baselines save failed")

    def base(self, tok: int, now: float | None = None):
        """-> [day_base, since, base_15m] (None where unknown). since = "HH:MM" when the day base is only
        the first OI seen after the open (no earlier day recorded for this contract)."""
        now = now or time.time()
        if tok in self.prev:
            day, since = self.prev[tok], None
        elif tok in self.first:
            day, hm = self.first[tok]
            since = hm if hm > self.open_hhmm else None
        else:
            day, since = None, None
        m15 = None
        h = self.hist.get(tok)
        if h:
            cut = now - WINDOW_S
            m15 = h[0][1]                               # less than 15 min of history: the oldest sample
            for t, v in h:
                if t >= cut - SAMPLE_S / 2:
                    m15 = v
                    break
        return [day, since, m15]

    def bases(self, toks, now: float | None = None) -> dict:
        now = now or time.time()
        return {str(t): self.base(t, now) for t in toks if t in self.first or t in self.prev}
