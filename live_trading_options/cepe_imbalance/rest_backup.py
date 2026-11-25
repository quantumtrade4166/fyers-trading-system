"""
cepe_imbalance/rest_backup.py - backup price source for the LIVE engine (25-Nov-2026 incident).

25-Nov 09:44:58 and again 09:57:45 IST Kotak's market-data stream went silent on every connection (both
accounts) while the sockets stayed up; SENSEX Premium LIVE held 6 legs, and its rules, MTM stop and trail sat
on frozen prices. The Kotak phone app kept updating, and Kotak's REST quote API keeps answering.

While the stream is silent (no tick for STALE_S) during market hours, this asks REST quotes() for what the live
books need - the index spot (by NAME: REST refuses the spot token) and the option strikes around each running
book's ATM + every held contract - once every POLL_S, and hands the answers to the engine as ordinary ticks.
The stream's own clock (feed.last_tick_at) is never touched, so the silence watchdog keeps reconnecting it; the
moment real ticks flow again this goes quiet. READ-ONLY (quotes only, never an order).
"""
import logging
import threading
import time

from live_trading_options.cepe_imbalance.feed import MultiFeed, Tick

log = logging.getLogger("cepe.restbackup")

STALE_S = 3.0          # stream silent this long -> poll
POLL_S = 1.0           # one round of quotes per second
CHUNK = 20             # instruments per quotes() call
STRIKES_EACH_SIDE = 6  # strikes around the book's ATM (strategy grid) per side


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class RestBackup:
    def __init__(self, engine, feed, sessions):
        self.E, self.feed, self.sessions = engine, feed, sessions
        self.active = False
        self.polls = 0
        self.ticks = 0
        self.last_poll_at = 0.0
        self.last_error = ""
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="CepeRestBackup").start()

    def stop(self):
        self._stop.set()

    def status(self) -> dict:
        return {"active": self.active, "polls": self.polls, "ticks": self.ticks,
                "last_poll_age": round(time.time() - self.last_poll_at, 1) if self.last_poll_at else None,
                "error": self.last_error}

    # ── which instruments ───────────────────────────────────────────────────
    def needed(self) -> list:
        """Feed keys the running / holding live books need now."""
        E, m = self.E, self.E.market
        keys = []
        for n, r in list(E.runners.items()):
            held = [p.key for p in r.book.positions.values() if p.lots]
            if not (r.running or held):
                continue
            keys += held
            u = r.und
            e = m.expiry(u, r.expiry_n)
            rk = m.ref_key(u, e)
            if rk:
                keys.append(rk)
            if not e:
                continue
            try:
                step = float(r.strategy.step(r.ctx))
            except Exception:
                step = 0.0
            atm = r.state.get("atm") or m.atm(u, e)
            if not atm or not step:
                continue
            for i in range(-STRIKES_EACH_SIDE, STRIKES_EACH_SIDE + 1):
                for kind in ("CE", "PE"):
                    c = m.contract(u, e, atm + i * step, kind)
                    if c:
                        keys.append(c.key)
        return list(dict.fromkeys(keys))

    # ── one round ───────────────────────────────────────────────────────────
    def poll(self, keys, now=None) -> list:
        """quotes() for these keys -> [Tick] (also returned for tests)."""
        now = now or time.time()
        client = self.sessions.get().client
        ticks = []
        for i in range(0, len(keys), CHUNK):
            part = keys[i:i + CHUNK]
            req, back = [], {}
            for k in part:
                seg, _, tok = k.partition("|")
                req.append({"instrument_token": tok, "exchange_segment": seg})
                back[str(tok)] = k
                back[str(tok).lower()] = k
            r = client.quotes(instrument_tokens=req, quote_type="all")
            rows = r if isinstance(r, list) else (r or {}).get("message") or (r or {}).get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                tok = str(row.get("exchange_token") or row.get("instrument_token") or "")
                k = back.get(tok) or back.get(tok.lower())
                ltp = _f(row.get("ltp"))
                if not k or not ltp or ltp <= 0:
                    continue
                d = row.get("depth") or {}
                b = (d.get("buy") or [{}])[0] if isinstance(d.get("buy"), list) else {}
                a = (d.get("sell") or [{}])[0] if isinstance(d.get("sell"), list) else {}
                bid, ask = _f(b.get("price")), _f(a.get("price"))
                bid = bid if bid and bid > 0 else None
                ask = ask if ask and ask > 0 else None
                upd = int(_f(row.get("lstup_time")) or 0)       # Kotak's own last-update time (epoch s)
                ticks.append(Tick(k, ltp, bid, ask,
                                  int(_f(b.get("quantity")) or 0) if bid else None,
                                  int(_f(a.get("quantity")) or 0) if ask else None,
                                  None, None, upd if 0 < upd <= now + 5 else int(now), now))
        return ticks

    def _loop(self):
        while not self._stop.wait(POLL_S):
            try:
                f = self.feed
                with f._lock:
                    wanted = list(f.wanted)
                silent = time.time() - (f.last_tick_at or 0) > STALE_S
                ka = getattr(self.E, "kite", None)
                if silent and ka is not None and ka.alive():   # Kite prices are flowing: no REST needed
                    silent = False
                if not (silent and wanted and MultiFeed.market_open(wanted)):
                    if self.active:
                        self.active = False
                        log.warning("stream is back - REST backup off after %d polls", self.polls)
                    continue
                keys = self.needed()
                if not keys:
                    continue
                if not self.active:
                    self.active = True
                    log.warning("stream silent %.0f s - REST backup prices ON (%d instruments, every %.0f s)",
                                time.time() - (f.last_tick_at or 0), len(keys), POLL_S)
                ticks = self.poll(keys)
                self.polls += 1
                self.last_poll_at = time.time()
                if ticks:
                    self.ticks += len(ticks)
                    self.E.on_ticks(ticks)
                self.last_error = ""
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.warning("REST backup: %s", self.last_error)
                self._stop.wait(5)
