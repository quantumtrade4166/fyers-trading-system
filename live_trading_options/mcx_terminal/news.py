"""
mcx_terminal/news.py — the NEWS button: headlines that move MCX (crude, gold, silver, gas), Trump's posts on
war / oil / tariffs / the Fed, and the week's big scheduled events (US crude stocks, CPI, Fed...).
================================================================================================

READ-ONLY: fetches public feeds, never touches orders. Runs on its own thread; a dead source only goes quiet.

Sources (all checked from the VPS 12-Nov-2026):
  * Google News RSS searches (personal-use feed)             every 2 min
  * oilprice.com main RSS                                       every 3 min
  * trumpstruth.org/feed  (unofficial archive of Truth Social)   every 60 s  -> kept only on a market keyword
  * ForexFactory's weekly calendar JSON (nfs.faireconomy.media)   every 30 min -> USD high impact + energy
Free feeds lag the wire by ~1-5 minutes: this explains a move, it does not beat it.

Each item: {id, ts (epoch), src, title, url, tags: [CRUDE|GAS|GOLD|SILVER|MACRO|WAR|TRADE], big, kind}
kind: "news" | "trump".  Events: {ts, title, country, impact, forecast, previous}.
Kept 48 h / 300 items in data/news.json (survives restarts).
"""
import datetime as dt
import email.utils
import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("mcx.news")
UA = {"User-Agent": "Mozilla/5.0 (compatible; desk-news/1.0)"}
KEEP_S = 48 * 3600
MAX_ITEMS = 300

GNEWS = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
QUERIES = [
    "crude oil OR OPEC OR Brent when:1d",
    "gold price OR silver price OR bullion when:1d",
    "(Iran OR Israel OR Russia OR Ukraine OR Hormuz OR Saudi) (attack OR strike OR ceasefire OR sanctions OR missile) when:1d",
    "Federal Reserve OR Fed rate OR US inflation OR Treasury yields when:1d",
    "natural gas price OR LNG when:1d",
    "Trump tariffs OR Trump oil OR Trump Iran when:1d",
]
SOURCES = [  # (name, kind, url or None for google, every_s, keep_only_tagged)
    *[("Google News", "news", GNEWS + urllib.parse.quote(q), 120, True) for q in QUERIES],
    ("oilprice.com", "news", "https://oilprice.com/rss/main", 180, False),
    ("Trump (Truth Social)", "trump", "https://www.trumpstruth.org/feed", 60, True),
]
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CAL_EVERY_S = 30 * 60

TAGS = {
    "CRUDE": r"crude|oil price|oil prices|brent|wti|opec|hormuz|refiner\w*|petroleum|barrels?|oil output|oil supply|"
             r"oil exports?|oil imports?|eia|oil",
    "GAS": r"natural gas|lng|henry hub|gas prices?",
    "GOLD": r"gold|bullion",
    "SILVER": r"silver",
    "MACRO": r"fed|federal reserve|rate hikes?|rate cuts?|interest rates?|inflation|cpi|powell|dollar|"
             r"treasury yields?|payrolls?|jobs report|recession|gdp",
    "TRADE": r"tariffs?|trade deal|trade war|export ban|embargo",
}
TAG_RE = {k: re.compile(r"\b(?:" + v + r")\b", re.I) for k, v in TAGS.items()}
# WAR (geopolitics) needs BOTH a place that moves oil / metals AND an action: "Ukraine winter aid" or Trump's
# "invasion of our country" (immigration) are not market news; "Iran deal talks" and "drone attack on Kyiv" are.
GEO_RE = re.compile(r"\b(?:iran\w*|israel\w*|ukrain\w*|russia\w*|kremlin|putin|tehran|hormuz|gulf|saudi\w*|uae|"
                    r"qatar\w*|houthis?|yemen\w*|red sea|iraq\w*|libya\w*|venezuel\w*|middle east|opec|nato|china|taiwan)\b", re.I)
ACT_RE = re.compile(r"\b(?:war|missiles?|strikes?|struck|attack\w*|ceasefire|truce|peace|deal|talks|negotiat\w*|"
                    r"sanction\w*|troops|drones?|military|invasion|invad\w*|nuclear|bomb\w*|airstrikes?|blockade|"
                    r"hostilit\w*|tankers?|seiz\w*|escalat\w*|retaliat\w*|threats?)\b", re.I)
_SHIP = r"(?:tankers?|vessels?|pipelines?|refiner\w*|oil field)"
_HIT = r"(?:hit|attack\w*|struck|seiz\w*|explosion|drones?)"
TANKER_RE = re.compile(rf"\b{_SHIP}\b.*\b{_HIT}\b|\b{_HIT}\b.*\b{_SHIP}\b", re.I)
GULF_RE = re.compile(r"\b(?:iran\w*|hormuz|gulf|saudi\w*|uae|qatar\w*|houthis?|red sea|iraq\w*|opec)\b", re.I)
MOVE_RE = re.compile(r"\b(?:surge\w*|soar\w*|plunge\w*|plummet\w*|crash\w*|tumble\w*|spike\w*|jump\w*|slump\w*|"
                     r"slides?|sink\w*|rall(?:y|ies)|record high|record low)\b|\d+(?:\.\d+)?%", re.I)
HARD_RE = re.compile(r"\b(?:attack\w*|strikes?|struck|missiles?|drones?|seiz\w*|blockade|war)\b", re.I)
POLICY_RE = re.compile(r"\b(?:opec\+?\s+(?:cuts?|hikes?|agrees?|raises?|boosts?)|rate hikes?|rate cuts?|hikes? rates|"
                       r"cuts? rates)\b", re.I)
MARKET_TAGS = {"CRUDE", "GAS", "GOLD", "SILVER", "MACRO", "WAR", "TRADE"}
COMMOD = {"CRUDE", "GAS", "GOLD", "SILVER"}


def tags_of(text: str) -> list:
    t = [k for k, r in TAG_RE.items() if r.search(text)]
    tanker = TANKER_RE.search(text)
    if tanker and "CRUDE" not in t:
        t.insert(0, "CRUDE")
    if (GEO_RE.search(text) and ACT_RE.search(text)) or tanker:
        t.append("WAR")
    return t


def is_big(text: str, tags: list, kind: str) -> bool:
    """Big = likely to move MCX now: a sharp move in a commodity, an attack on tankers / pipelines / the Gulf,
    OPEC or Fed rate action, or a Trump post on oil / war / tariffs / the Fed."""
    tg = set(tags)
    if kind == "trump":
        return bool(tg & (COMMOD | {"WAR", "TRADE", "MACRO"}))
    return bool((tg & COMMOD and MOVE_RE.search(text)) or TANKER_RE.search(text)
                or ("WAR" in tg and GULF_RE.search(text) and HARD_RE.search(text)) or POLICY_RE.search(text))


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())[:90]


def parse_rss(xml_bytes: bytes, src: str, kind: str, keep_only_tagged: bool) -> list:
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter("item"):
        title = _clean(it.findtext("title"))
        desc = _clean(it.findtext("description"))
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        try:
            ts = email.utils.parsedate_to_datetime(pub).timestamp() if pub else time.time()
        except Exception:
            ts = time.time()
        source = src
        if kind == "trump":
            if not title or title.lower().startswith("[no title]") or len(desc) > len(title):
                title = desc or title
            if not title:
                continue
            title = title[:400]
        else:
            s_el = it.find("source")
            if s_el is not None and s_el.text:                      # Google: "Headline - Outlet"
                source = s_el.text.strip()
                if title.endswith(" - " + source):
                    title = title[: -len(source) - 3]
        if not title:
            continue
        tg = tags_of(title + " " + (desc if kind == "trump" else ""))
        if keep_only_tagged and not (set(tg) & MARKET_TAGS):
            continue
        if src == "oilprice.com" and not tg:
            tg = ["CRUDE"]
        big = is_big(title, tg, kind)
        out.append({"id": _key(title), "ts": round(ts), "src": source, "title": title, "url": link,
                    "tags": tg, "big": big, "kind": kind})
    return out


def parse_calendar(raw: bytes) -> list:
    out = []
    for e in json.loads(raw):
        t, c, imp = e.get("title", ""), e.get("country", ""), e.get("impact", "")
        energy = re.search(r"crude|natural gas|opec|oil", t, re.I)
        if not ((c == "USD" and imp == "High") or energy):
            continue
        try:
            ts = dt.datetime.fromisoformat(e["date"]).timestamp()
        except Exception:
            continue
        out.append({"ts": round(ts), "title": t, "country": c, "impact": imp,
                    "forecast": e.get("forecast", ""), "previous": e.get("previous", "")})
    return sorted(out, key=lambda x: x["ts"])


class NewsDesk:
    def __init__(self, path, on_new=None, fetch=None):
        """on_new(list_of_new_items, events_or_None) - called on the news thread. fetch(url) -> bytes (tests)."""
        self.path = path
        self.on_new = on_new or (lambda items, events: None)
        self.fetch = fetch or self._fetch
        self.items: list[dict] = []
        self.events: list[dict] = []
        self.ids: set[str] = set()
        self.status: dict[str, str] = {}          # source -> "ok HH:MM" / "error ..."
        self.lock = threading.Lock()
        self._next: dict[str, float] = {}
        self._load()

    @staticmethod
    def _fetch(url: str) -> bytes:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            return r.read()

    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                self.items, self.events = d.get("items", []), d.get("events", [])
                self.ids = {i["id"] for i in self.items}
        except Exception:
            log.exception("news load failed; starting empty")

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": self.items, "events": self.events}), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("news save failed")

    def add(self, found: list) -> list:
        """Merge freshly parsed items; -> the ones not seen before (newest first)."""
        now = time.time()
        new = []
        with self.lock:
            for it in found:
                if it["id"] in self.ids or now - it["ts"] > KEEP_S:
                    continue
                self.ids.add(it["id"])
                new.append(it)
            if new:
                self.items = sorted(self.items + new, key=lambda x: -x["ts"])
                self.items = [i for i in self.items if now - i["ts"] <= KEEP_S][:MAX_ITEMS]
                # ids keep every headline seen this run, so one trimmed off the list never returns as "new"
        return sorted(new, key=lambda x: -x["ts"])

    def poll_once(self, now: float | None = None):
        now = now or time.time()
        new, events = [], None
        for name, kind, url, every, only_tagged in SOURCES:
            if self._next.get(url, 0) > now:
                continue
            self._next[url] = now + every
            try:
                new += self.add(parse_rss(self.fetch(url), name, kind, only_tagged))
                self.status[name] = "ok " + dt.datetime.now().strftime("%H:%M")
            except Exception as e:
                self.status[name] = f"error {type(e).__name__}"
                log.warning("news source %s: %s", name, e)
        if self._next.get(CAL_URL, 0) <= now:
            self._next[CAL_URL] = now + CAL_EVERY_S
            try:
                events = parse_calendar(self.fetch(CAL_URL))
                with self.lock:
                    self.events = events
                self.status["Calendar"] = "ok " + dt.datetime.now().strftime("%H:%M")
            except Exception as e:
                self.status["Calendar"] = f"error {type(e).__name__}"
                log.warning("news calendar: %s", e)
        if new or events is not None:
            self._save()
            self.on_new(sorted(new, key=lambda x: -x["ts"]), events)
        return new

    def snapshot(self) -> dict:
        with self.lock:
            return {"t": "news", "full": True, "items": list(self.items), "events": list(self.events),
                    "status": dict(self.status)}

    def run(self):
        while True:
            try:
                self.poll_once()
            except Exception:
                log.exception("news poll")
            time.sleep(15)

    def start(self):
        threading.Thread(target=self.run, daemon=True, name="News").start()
        return self
