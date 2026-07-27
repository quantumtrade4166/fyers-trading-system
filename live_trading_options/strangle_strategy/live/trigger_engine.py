"""
live/trigger_engine.py — the real-time strategy state machine.
=============================================================

Same rules as core.signal_engine.simulate_day (the trusted paper logic), but driven
LIVE: entry fills are detected TICK-by-TICK (fire the instant combined premium hits
`low-1`), while signals / exits / cancel-replace happen at 5-min candle close.

It emits decisions via two callbacks so the SAME engine drives paper or live:
    on_entry(price, cycle, reason)   on_exit(price, cycle, reason)
In paper mode these just record; in live mode they go strategy -> guard -> executor.

Rules recap: strikes fixed at 9:20; ONE position at a time, strict alternation up to
`max_entries` cycles. ENTRY = a red candle closing below VWAP arms a sell at low-1;
it FILLS the moment premium trades to low-1. EXIT = a candle closes above VWAP -> buy
back at that close. No new entry after `entry_cutoff`; force square-off at `square_off`.
"""

import datetime as dt


def _t(s: str) -> dt.time:
    return dt.datetime.strptime(s, "%H:%M").time()


def _floor5(hm: str) -> str:
    h, m = map(int, hm.split(":"))
    return f"{h:02d}:{(m // 5) * 5:02d}"


class _Pending:
    __slots__ = ("trigger", "sig_time")

    def __init__(self, trigger, sig_time):
        self.trigger, self.sig_time = trigger, sig_time


class LiveTrigger:
    def __init__(self, on_entry, on_exit, *, max_entries: int = 4,
                 entry_cutoff: str = "14:30", square_off: str = "15:15"):
        self.on_entry, self.on_exit = on_entry, on_exit
        self.max_entries = max_entries
        self.cutoff, self.sq = _t(entry_cutoff), _t(square_off)
        self.entries = 0
        self.in_pos = False
        self.pending: _Pending | None = None
        self.done = False
        self._entered_candle = None      # bucket of the fill (skip exit that same candle)

    # ── TICK: the only place an ENTRY fills (the low-1 trigger) ───────────
    def on_tick(self, premium: float, hm: str):
        """A live tick. Fires an entry the instant premium reaches the resting
        trigger, while flat and before the cutoff."""
        if self.done or self.in_pos or self.pending is None:
            return
        if _t(hm) > self.cutoff:
            return
        if premium <= self.pending.trigger:
            self.entries += 1
            self._entered_candle = _floor5(hm)
            price = round(self.pending.trigger, 2)
            self.on_entry(price, self.entries,
                          f"limit {price} (signal {self.pending.sig_time}) hit")
            self.in_pos = True
            self.pending = None

    # ── CANDLE CLOSE: exits, square-off, signal arming / cancel / replace ──
    def on_candle_close(self, candle: dict):
        """`candle` needs time (HH:MM), open, high, low, close, vwap."""
        if self.done:
            return
        hm = candle["time"]
        tt = _t(hm)
        o, c, low = float(candle["open"]), float(candle["close"]), float(candle["low"])
        vwap = float(candle["vwap"])
        is_red, below, above = c < o, c < vwap, c > vwap

        # manage an open position
        if self.in_pos:
            if self._entered_candle == hm:      # just entered this candle -> don't also exit it
                return
            if tt >= self.sq:                   # force square-off at 3:15 (at this candle's open)
                self.on_exit(round(o, 2), self.entries, f"square-off {self.sq:%H:%M}")
                self.in_pos = False
                self.done = True
                return
            if above:                           # exit: close above VWAP -> buy back at close
                self.on_exit(round(c, 2), self.entries, "close above VWAP")
                self.in_pos = False
            return

        # flat with a resting trigger that did NOT fill during this candle -> cancel/replace
        if self.pending is not None:
            if above:
                self.pending = None                                   # cancel
            elif is_red and below and self.entries < self.max_entries and tt <= self.cutoff:
                self.pending = _Pending(round(low - 1, 2), hm)        # replace at new low-1
            else:
                self.pending = None                                   # cancel
            return

        # flat, no trigger: a fresh red-below-VWAP candle arms one
        if self.entries < self.max_entries and tt <= self.cutoff and is_red and below:
            self.pending = _Pending(round(low - 1, 2), hm)
