"""
vwap_engine.py — BTC VWAP Strangle, every version on one feed.
=============================================================

    ist_day      09:30 -> 17:10 IST, today's expiry
    full_expiry  17:35 -> 17:10 IST next day, tomorrow's expiry
    ist_live     "Vwap New Paper": split-leg exits (vwap/split_book.py), PAPER forever
    ist_cap      "Vwap 1.5x Paper": the same rules + the busted leg out the moment the
                 combined premium reaches 1.5x VWAP (vwap_cap_mult), PAPER only
    ist_delta    "Vwap New Delta Ex": the same rules, PAPER until armed via
                 data/vwap_state/live_control_ist_delta.json (dashboard), then LIVE

The NIFTY/SENSEX Vwap Strangle rules on Delta Exchange BTC daily options:
strikes with combined premium <= 100, 5-min combined candles + VWAP, sell on a
red-below-VWAP candle's low - 1, buy back on a close above VWAP.

Prices:
  REST  the whole chain every `poll_seconds`, both legs from ONE snapshot — strike
        selection, candles, the entry trigger, square-off, MTM stop. Candles use
        the NIFTY smoothed-wick method (premium_builder Method A): 5-min high/low
        from each minute's synchronized open and close points only.
  PUSH  spot + our legs' marks for the ticker file only — never candles or the
        trigger, because the two legs arrive at different instants.

Run:
    .venv/Scripts/python.exe live_trading_options/delta_btc/vwap_engine.py

Safe to restart any time: each version resumes its cycle from
data/vwap_state/{version}_resume.json. Lock port 47657 prevents a duplicate.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
import time
import threading
import traceback

from core.shared import singleton, now_ist, LOGS
from core.api import btc_options, btc_option_tickers
from core.chain import LiveChain
from vwap.book import VwapBook, atomic_write
from vwap.split_book import SplitVwapBook

PORT_BTC_VWAP_ENGINE = 47657

PARAMS = json.loads((ROOT / "config" / "vwap_parameters.json").read_text(encoding="utf-8"))
POLL = float(PARAMS.get("poll_seconds", 2))
STALL = int(PARAMS.get("stall_seconds", 180))
SPAN = int(PARAMS.get("chain_span", 45))
SETTLE = PARAMS.get("settlement_time_ist", "17:30")
STATE_DIR = ROOT / "data" / "vwap_state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

_log_path = LOGS / "vwap_engine.log"
_lock = threading.RLock()
_chains: dict = {}
_chain_loaded: dict = {}      # expiry code -> the product-master timestamp it was built from
_products = {"rows": [], "at": 0.0}
PRODUCTS_EVERY = 600          # seconds between product-master refreshes


def log(msg: str):
    line = f"{now_ist():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def products(force=False):
    """The BTC product master (every listed strike of every expiry).

    Refreshed every 10 minutes, not once a day: Delta LISTS NEW STRIKES during the
    day as spot moves. On 2026-09-19 the 19-Sep chain was loaded at 17:35 the day
    before with 31 strikes; by 09:30 BTC had run to the top of that grid, the
    strikes above it were listed but unknown to the engine, and ist_day found "no
    equidistant pair <= 100" all day while 81800/80600 sat at 79.6."""
    if force or not _products["rows"] or time.time() - _products["at"] > PRODUCTS_EVERY:
        _products["rows"] = btc_options()
        _products["at"] = time.time()
    return _products["rows"]


def get_chain(code, fresh=False):
    """The LiveChain for one expiry. Re-reads its strike list whenever the product
    master has been refreshed since it was built (load() only ADDS contracts and
    keeps every mark it already has, so this is safe with a position open).
    `fresh=True` forces a product refresh first — used right before strike
    selection, the one moment a stale grid changes a decision."""
    if not code:
        return None
    if fresh:
        products(force=True)
    if code in _chains and _chain_loaded.get(code, 0) < _products["at"]:
        before = len(_chains[code].strikes)
        try:
            _chains[code].load(products())
            _chain_loaded[code] = _products["at"]
            if len(_chains[code].strikes) != before:
                log(f"  chain {code}: strikes {before} -> {len(_chains[code].strikes)} (new listings)")
        except RuntimeError:
            pass
    if code not in _chains:
        try:
            _chains[code] = LiveChain(code, span=SPAN, settle_ist=SETTLE).load(products())
        except RuntimeError:
            try:
                _chains[code] = LiveChain(code, span=SPAN, settle_ist=SETTLE).load(
                    products(force=True))
            except RuntimeError:
                return None
        _chain_loaded[code] = _products["at"]
        log(f"  chain {code}: {len(_chains[code].strikes)} strikes")
    return _chains[code]


def main():
    if not singleton.acquire(PORT_BTC_VWAP_ENGINE):
        log("another BTC VWAP engine holds the lock — this duplicate exits.")
        return

    names = [n for n, v in PARAMS["versions"].items() if v.get("enabled", True)]
    # split_legs versions use SplitVwapBook; only one with live_capable (ist_delta)
    # can ever trade LIVE, once armed from its control file
    books = {n: (SplitVwapBook if PARAMS["versions"][n].get("split_legs") else VwapBook)(
        n, PARAMS, ROOT, log=log) for n in names}
    log(f"BTC VWAP engine start — versions {names}, poll {POLL}s "
        f"(live-capable: {[n for n, b in books.items() if getattr(b, 'live_capable', False)]})")
    for b in books.values():
        s = b.s
        log(f"  {b.name}: {s.start:%H:%M}->{s.square_off:%H:%M} cutoff {s.cutoff:%H:%M} "
            f"combined<={s.threshold:g} x{s.contracts} max {s.max_entries} "
            f"mtm stop ${s.mtm_stop:g}")

    feed = None

    def on_push():
        # Push ticks only refresh the ticker file. They are NOT fed into candles or
        # the trigger: the socket delivers CE and PE marks at different instants,
        # and adding two unsynchronized legs invents combined prices that never
        # existed (the oversized wicks of the first build). Candles and the trigger
        # use the REST snapshot, where both legs come from the same instant.
        try:
            with _lock:
                write_tick(books, feed)
        except Exception as e:
            log(f"  push handler error: {type(e).__name__}: {e}")

    try:
        from live.ws_feed import WSFeed
        feed = WSFeed(on_tick=on_push).start()
        log("  push feed started")
    except Exception as e:
        log(f"  push feed unavailable ({e}) — REST only")

    last_good = time.monotonic()
    polls = 0
    while True:
        started = time.monotonic()
        now = now_ist()
        try:
            tickers = btc_option_tickers()
            with _lock:
                wanted = set()
                for b in books.values():
                    if b.active:
                        wanted.add(b.expiry)
                    else:
                        bd = b.s.bounds(now)
                        if bd:
                            from core.chain import expiry_code
                            wanted.add(expiry_code(b.s.expiry_date(bd)))
                for code in wanted:
                    ch = get_chain(code)
                    if ch is not None:
                        ch.refresh(tickers)
                for name, b in books.items():
                    try:
                        b.on_poll(now, get_chain)
                    except Exception as e:
                        log(f"  !! {name} step error: {type(e).__name__}: {e}")
                        log(traceback.format_exc(limit=6))
                for code in list(_chains):
                    if code not in wanted and _chains[code].seconds_to_settlement(now) < -600:
                        _chains.pop(code, None)
                if feed is not None:
                    feed.track([s for b in books.values() if b.active
                                for s in (b.pair["ce_symbol"], b.pair["pe_symbol"])])
                write_tick(books, feed)
            last_good = time.monotonic()
            polls += 1
            if polls % 150 == 0:
                log("  poll %d  %s" % (polls, " | ".join(
                    f"{n}:{b.status} comb {b.last['combined']} vwap {b.builder.live_vwap() if b.builder else None} "
                    f"{'IN' if b.pos else 'flat'} mtm ${b.mtm():+.2f}" for n, b in books.items())))
        except Exception as e:
            log(f"  poll failed: {type(e).__name__}: {e}")

        if time.monotonic() - last_good > STALL:
            log(f"  no good poll for {STALL}s — exiting for a clean restart")
            os._exit(1)
        time.sleep(max(0.0, POLL - (time.monotonic() - started)))


def write_tick(books, feed):
    """Tiny file the tab polls every second: live combined, VWAP, MTM per version."""
    try:
        out = {"ts": now_ist().strftime("%H:%M:%S"),
               "feed": bool(feed and feed.status().get("live")),
               "spot": feed.spot() if feed else None, "versions": {}}
        for n, b in books.items():
            out["versions"][n] = {
                "status": b.status, "last": b.last, "mtm": b.mtm(),
                "mode": getattr(b, "mode", "paper"),
                # split books: net P&L after ALL charges (fees + GST, open legs at the
                # buy-back ask less the estimated exit fee) next to the price P&L
                "net_mtm": (round(b.realized() + b.unrealized(), 4) if hasattr(b, "charges") else None),
                "charges": (b.charges() if hasattr(b, "charges") else None),
                "realized": b.realized(), "in_pos": b.pos is not None,
                "vwap": b.builder.live_vwap() if b.builder else None,
                "pending": b.trigger.pending if b.trigger else None,
                "forming": ({"t": b.builder.cur["start"].strftime("%Y-%m-%d %H:%M"),
                             "o": b.builder.cur["open"], "h": b.builder.cur["high"],
                             "l": b.builder.cur["low"], "c": b.builder.cur["close"]}
                            if b.builder and b.builder.cur else None),
            }
        atomic_write(STATE_DIR / "TICK.json", json.dumps(out, default=str), tries=2)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("interrupted — exiting.")
