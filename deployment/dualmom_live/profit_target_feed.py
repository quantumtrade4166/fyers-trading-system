"""
profit_target_feed.py - TICK-DRIVEN profit target for the Kotak Rohit book.

WHY NOT POLLING
    profit_target.check() prices the book over REST, so it can only run on a
    timer, and a timer is the wrong shape for a trigger: between ticks the P&L is
    unknown, and the target can be crossed and gone before the next look. This
    module recomputes the combined MTM on EVERY tick instead - measured ~39
    ticks/sec across 40 names - and fires the moment the condition is met.

    Prices come from KOTAK's own market-data websocket (mcx_terminal/feed.py,
    reused as-is), NOT from Kite. That matters: Zerodha allows 3 websockets per
    API key and all 3 are already in use by DN / ST / Pivot, so a Kite socket
    here would be refused and could knock a live engine offline. Kotak's feed is
    a different broker entirely and costs us nothing we already own. It also
    means ZERO REST calls for prices - REST is used only to refresh positions
    every few minutes and to send the orders.

    Verified live 2026-11-11: all 40 held names resolve to nse_cm tokens, the
    feed authenticates on the Rohit session, and feed LTP matches REST quotes to
    the paisa.

ONE SESSION
    The feed needs `sid` + `feedUrl`, which appear ONLY in the totp_validate
    response. kotak_auth_dm.login() now stashes them on the client as
    `dm_feed`, so this reuses the service's existing Rohit session rather than
    opening a second one (Kotak allows one session per account).

WHY A CONFIRM WINDOW
    The target is Rs 2,000 on a ~Rs 9.8L book - about 0.2%, well inside normal
    noise. ATHERENERG alone is ~Rs 90k of exposure, so one outlier print moving
    it 2.5% swings the combined total by Rs 2,250 and trips the target by itself.
    So the condition must HOLD for `confirm_seconds` (default 3) before firing.
    Set it to 0 for raw instant. This is the difference between fast and twitchy.

PRE-RESOLVED, SO FIRING IS NOT A ROUND TRIP
    Every held name's Kotak trading symbol and tick size is resolved ONCE at
    start and cached. When the target trips, the sells go straight to
    place_limit - no search_scrip, no quotes. Resolution is also how the 1-Oct
    exit died (HFCL-BE), so doing it up front means a resolve problem surfaces
    while nothing is pending rather than halfway through an exit.

SAFETY
    * owner arms it via dualmom_live_state/profit_target.json (shared with the
      polling path); disarmed, this module does not even open the feed.
    * it will not fire unless EVERY position has a live tick. A partial sum is
      not the book's P&L.
    * a stale feed (no tick for `stale_seconds`) stops it arming, loudly: a
      frozen price that happens to sit above the target must not fire.
    * one shot - a completed flatten stamps `fired`; a HALTED one does not, so
      it retries.
"""

import threading
import time
from datetime import datetime

from deployment.dualmom_live import book as B
from deployment.dualmom_live import kotak_equity as K
from deployment.dualmom_live import profit_target as PT

CONFIRM_SECONDS = 3.0        # condition must hold this long before firing
STALE_SECONDS = 15.0         # no tick in this long -> refuse to fire
POSITION_REFRESH = 300.0     # re-read qty/avg from REST this often


class _Holder:
    """kotak_session.SessionHolder shape over a DualMom client FACTORY.

    A factory, not a client, and this is the difference between working tomorrow
    and silently stopping after today.

    Kotak sessions are daily. feed.py handles that correctly: on an auth failure
    it sets _force_login and calls sessions.get(force=True) expecting a NEW
    session. An earlier version of this class snapshotted sid/feedUrl at
    construction and returned them forever, so the feed would have retried
    against a dead session every morning and only recovered when the supervisor
    noticed a stale feed 60s later - a daily outage, and exactly the sort of
    quiet failure the rest of this module is written to avoid.

    So every get() reads the CURRENT client's dm_feed, and force=True asks the
    shared session for a real reconnect.
    """

    class _S:
        pass

    def __init__(self, get_client):
        self._get_client = get_client
        self._check(get_client(False))

    @staticmethod
    def _check(client):
        f = getattr(client, "dm_feed", None) or {}
        if not f.get("sid") or not f.get("feed_url"):
            raise RuntimeError(
                "this Kotak client carries no feed credentials - log in through "
                "kotak_auth_dm.login() (it stashes sid/feedUrl as client.dm_feed)")
        return f

    def get(self, force: bool = False):
        client = self._get_client(bool(force))
        f = self._check(client)
        s = _Holder._S()
        s.client = client
        s.sid = f["sid"]
        s.feed_url = f["feed_url"]
        s.ucc = f.get("ucc")
        s.at = f.get("at") or time.time()
        return s


class ProfitTargetFeed:
    """Streams the book and flattens it when combined MTM reaches the target."""

    def __init__(self, client, on_log=None, confirm_seconds=CONFIRM_SECONDS):
        """`client` may be a Kotak client OR a factory f(reconnect: bool) -> client.

        Pass the factory in long-running use: REST calls and the feed then both
        follow the shared session across its daily re-login, instead of pinning
        a client that stops working overnight.
        """
        if callable(client):
            self._client_factory = client
        else:
            self._client_factory = lambda reconnect=False, _c=client: _c
        self.log = on_log or (lambda m: None)
        self.confirm_seconds = float(confirm_seconds)
        self.pos = {}            # symbol -> {qty, avg_price}
        self.tok_of = {}         # symbol -> kotak token
        self.sym_of = {}         # token  -> symbol
        self.meta = {}           # symbol -> {trading_symbol, tick_size, series}
        self.ltp = {}            # token  -> (price, epoch)
        self.feed = None
        self.pnl = None
        self.above_since = None
        self.last_tick_at = 0.0
        self.fired = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._pos_at = 0.0

    # -- setup ---------------------------------------------------------------
    @property
    def client(self):
        """The CURRENT shared Kotak session (re-logged-in daily by _get_client)."""
        return self._client_factory(False)



    def load_positions(self):
        """qty/avg from broker_positions (nets today's trades), plus pre-resolve."""
        pos = {s: {"qty": int(p["qty"]), "avg_price": float(p.get("avg_price") or 0.0)}
               for s, p in B.broker_positions(self.client).items() if int(p["qty"]) > 0}
        added, failed = [], []
        for s in pos:
            if s in self.meta:
                continue
            try:
                r = K.resolve(self.client, s)
                self.tok_of[s] = int(r["token"])
                self.sym_of[int(r["token"])] = s
                self.meta[s] = {"trading_symbol": r["trading_symbol"],
                                "tick_size": r["tick_size"], "series": r["series"]}
                added.append(s)
            except Exception as e:
                failed.append(f"{s} ({K._etxt(e)[:60]})")
        with self._lock:
            self.pos = pos
            self._pos_at = time.time()
        if failed:
            # Unresolvable now means unsellable later. Say so while nothing is
            # pending, rather than discovering it mid-exit.
            self.log(f"profit target: {len(failed)} name(s) will NOT resolve, so a "
                     f"flatten would halt: {'; '.join(failed)}")
        return pos, added, failed

    # -- the tick path -------------------------------------------------------

    def _on_ticks(self, ticks):
        now = time.time()
        with self._lock:
            for t in ticks:
                tok, px = t[0], t[1]
                if px:
                    self.ltp[tok] = (float(px), now)
            self.last_tick_at = now
            state = self._evaluate_locked(now)
        if state == "fire":
            self._fire()

    def _evaluate_locked(self, now):
        """Compute MTM and decide. Caller holds the lock."""
        if not self.pos or self.fired:
            return None
        total, missing = 0.0, []
        for s, p in self.pos.items():
            tok = self.tok_of.get(s)
            got = self.ltp.get(tok) if tok else None
            if not got or not p["avg_price"]:
                missing.append(s)
                continue
            total += p["qty"] * (got[0] - p["avg_price"])
        if missing:
            self.pnl = None
            self.above_since = None
            return None
        self.pnl = round(total, 2)

        ctl = PT.control()
        if not ctl["armed"] or ctl.get("fired"):
            self.above_since = None
            return None
        if total < ctl["target_rs"]:
            self.above_since = None
            return None
        if now - self.last_tick_at > STALE_SECONDS:
            self.above_since = None
            return None
        if self.above_since is None:
            self.above_since = now
            return None
        if now - self.above_since < self.confirm_seconds:
            return None
        self.fired = True        # latch inside the lock so one tick fires once
        return "fire"

    # -- firing --------------------------------------------------------------

    def _fire(self):
        with self._lock:
            pnl, pos = self.pnl, dict(self.pos)
            marks = {s: self.ltp[self.tok_of[s]][0] for s in pos
                     if self.tok_of.get(s) in self.ltp}
        reason = (f"combined MTM Rs {pnl:,.2f} >= target held for "
                  f"{self.confirm_seconds:.0f}s on {len(pos)} positions (tick feed)")
        self.log(f"PROFIT TARGET FIRED: {reason}")
        try:
            run = PT.flatten(self.client, reason, marks=marks, holds=pos)
        except Exception as e:
            self.fired = False       # nothing was sent; let it retry
            self.log(f"profit target flatten raised: {K._etxt(e)[:200]}")
            return
        orders = run.get("orders") or []
        filled = [o for o in orders if str(o.get("status")) == "FILLED"]
        done = bool(orders) and not run.get("halted") and len(filled) == len(orders)
        for line in run.get("log", []):
            self.log(f"  {line}")
        self.log(f"profit target: {len(filled)}/{len(orders)} filled, complete={done}"
                 + (f", HALTED: {run.get('halted')}" if run.get("halted") else ""))
        if done:
            PT._write({"fired": datetime.now().isoformat(timespec="seconds"),
                       "fired_pnl": pnl, "fired_reason": reason})
            self._stop.set()
        else:
            self.fired = False       # retry on a later tick
            self.above_since = None

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        from live_trading_options.mcx_terminal import feed as F
        pos, added, failed = self.load_positions()
        if not pos:
            self.log("profit target: nothing held, not starting the feed")
            return False
        self.feed = F.KotakFeed(_Holder(self._client_factory), self._on_ticks,
                                on_status=lambda s: None,
                                seg_of=lambda t: "nse_cm")
        self.feed.start()
        self.feed.want(sorted(self.tok_of.values()))
        self.log(f"profit target feed: {len(pos)} positions, "
                 f"{len(self.tok_of)} subscribed, confirm {self.confirm_seconds:.0f}s")
        threading.Thread(target=self._housekeep, daemon=True,
                         name="ptf-housekeep").start()
        return True

    def _housekeep(self):
        """Refresh qty/avg periodically; positions change rarely but they DO."""
        while not self._stop.wait(30.0):
            if time.time() - self._pos_at < POSITION_REFRESH:
                continue
            try:
                _, added, _ = self.load_positions()
                if added:
                    self.feed.want([self.tok_of[s] for s in added])
            except Exception as e:
                self.log(f"profit target position refresh failed: {K._etxt(e)[:160]}")

    def stop(self):
        self._stop.set()
        if self.feed:
            self.feed.stop()

    def status(self) -> dict:
        with self._lock:
            ctl = PT.control()
            held = dict(self.pos)
            priced = sum(1 for s in held if self.tok_of.get(s) in self.ltp)
            return {
                "armed": ctl["armed"], "target_rs": ctl["target_rs"],
                "fired": ctl.get("fired"), "pnl": self.pnl,
                "gap": None if self.pnl is None else round(ctl["target_rs"] - self.pnl, 2),
                "positions": len(held), "priced": priced,
                "above_for": None if not self.above_since else round(time.time() - self.above_since, 1),
                "confirm_seconds": self.confirm_seconds,
                "last_tick_age": None if not self.last_tick_at else round(time.time() - self.last_tick_at, 1),
                "feed": self.feed.status() if self.feed else None,
            }
