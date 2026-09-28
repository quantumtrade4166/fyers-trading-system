"""
vwap/book.py — one version's paper book: strikes, candles, trigger, fills, P&L.
===============================================================================

One VwapBook per version (ist_day, full_expiry). The engine calls:

    on_poll(now, get_chain)          every REST poll (~2s): cycle roll, sampling,
                                     candle close, MTM stop, square-off, persistence
    on_sample(now, ce, pe)           every push tick for our two legs: sampling and
                                     the tick-exact entry trigger

Fills are PAPER, priced like a real market order: the contract's live L2 book is
walked at full size (core/fills.py via LiveChain.book_fill) and Delta's fee is
charged per side. The trigger level is recorded next to the real fill so the gap
(the slippage the NSE live tab measures) is visible on every trade.

Safety rules carried over from the NSE book:
  - never write a fill you have not seen: an exit leg with no price stays OPEN and
    is retried every poll
  - a half-filled entry is unwound at once — never left single-legged
  - a restart resumes the cycle from disk (strikes, candles, VWAP, position)
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import time
import datetime as dt
from pathlib import Path

from core.api import candles as api_candles
from core.chain import expiry_code
from vwap.strategy import Session, CandleBuilder, Trigger, select_pair, bucket_start

IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def atomic_write(path: Path, text: str, tries: int = 5):
    """Write via a temp file + rename. On Windows the rename is refused while
    another process (a dashboard read, an indexer, antivirus) has the target open,
    so retry briefly and, as a last resort, write in place — a stale resume file
    is far worse than a non-atomic one."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for i in range(tries):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    path.write_text(text, encoding="utf-8")
    try:
        tmp.unlink()
    except Exception:
        pass


def _epoch(ist_naive: dt.datetime) -> int:
    return int((ist_naive - IST_OFFSET).replace(tzinfo=dt.timezone.utc).timestamp())


class VwapBook:
    def __init__(self, name: str, params: dict, root: Path, log=print):
        self.name = name
        self.params = params
        self.s = Session(name, params["versions"][name])
        self.offset = float(params.get("trigger_offset", 1.0))
        self.minutes = int(params.get("candle_minutes", 5))
        fees = params.get("fees") or {}
        self.taker_rate = float(fees.get("taker_rate_notional", 0.0001))
        self.premium_cap = float(fees.get("premium_cap_rate", 0.035))
        self.max_slip = float(params.get("max_entry_slippage_pct", 5)) / 100.0
        self._last_reject = None
        self._last_fresh = None
        self.log_line = log

        self.state_dir = root / "data" / "vwap_state"
        self.results = root / "data" / "vwap_results"
        self.charts = self.results / "charts"
        self.logs = root / "logs"
        for d in (self.state_dir, self.results, self.charts, self.logs):
            d.mkdir(parents=True, exist_ok=True)

        self._reset()
        self._last_equity_min = None

    # ── cycle state ──────────────────────────────────────────────────────
    def _reset(self):
        self.key = None
        self.bounds = None
        self.expiry = None
        self.pair = None             # selection dict + symbols
        self.late_start = False
        self.builder = None
        self.trigger = None
        self.pos = None              # open trade dict, or None
        self.trades = []             # closed trades this cycle
        self.status = "waiting"
        self.stop_reason = None
        self.last = {"ce": None, "pe": None, "combined": None, "at": None}
        self.select_error = None

    @property
    def active(self) -> bool:
        return self.key is not None

    def _now_str(self, now):
        return now.strftime("%Y-%m-%d %H:%M:%S")

    def audit(self, now, event: str, **fields):
        rec = {"ts": self._now_str(now), "version": self.name, "cycle": self.key,
               "event": event, **fields}
        try:
            with open(self.logs / f"{now:%Y-%m-%d}_btc_vwap_audit.log", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass
        bits = " ".join(f"{k}={v}" for k, v in fields.items())
        self.log_line(f"  [{self.name}] {event} {bits}")

    # ── engine entry points ──────────────────────────────────────────────
    def on_poll(self, now: dt.datetime, get_chain):
        b = self.s.bounds(now)

        # the cycle is over (square-off passed) or a new one has begun
        if self.active and (b is None or Session.key(b) != self.key):
            self._end_cycle(now, get_chain, "square-off")

        if b is None:
            self.status = "waiting"
            return

        if not self.active:
            self._start_cycle(now, b, get_chain)
            if not self.active:
                return

        chain = get_chain(self.expiry)
        if chain is None:
            return
        self.bind(now, chain)

        ce = chain.mark.get((self.pair["ce"], "CE"))
        pe = chain.mark.get((self.pair["pe"], "PE"))
        self._sample(now, ce, pe, chain)

        closed = self.builder.due(now)
        if closed:
            self._on_close(now, closed, chain)

        if self.pos and self.pos.get("exit_pending"):
            self._try_exit(now, chain, self.pos["exit_pending"])

        self._check_mtm_stop(now, chain)
        self._record_equity(now)
        self.write_state(now)

    def on_sample(self, now: dt.datetime, ce: float, pe: float, get_chain):
        """A push tick for our legs — the tick-exact path."""
        if not self.active or ce is None or pe is None:
            return
        chain = get_chain(self.expiry)
        if chain is None:
            return
        self.bind(now, chain)
        self._sample(now, ce, pe, chain)

    # ── cycle start / end ────────────────────────────────────────────────
    def _start_cycle(self, now, b, get_chain):
        key = Session.key(b)
        if self._restore(now, key):
            return
        code = expiry_code(Session.expiry_date(b))
        # Selection must see TODAY's listed grid, not the one from when the chain
        # was first built — at most one forced product refresh a minute.
        fresh = (self._last_fresh is None or (now - self._last_fresh).total_seconds() >= 60)
        if fresh:
            self._last_fresh = now
        try:
            chain = get_chain(code, fresh=fresh)
        except TypeError:                    # a getter without the keyword (tests)
            chain = get_chain(code)
        if chain is None or not chain.is_ready():
            self.status = f"waiting for chain {code}"
            return
        pick = select_pair(chain.mark, chain.strikes, chain.spot, self.s.threshold,
                           fallback_last=self.s.fallback_last)
        if pick is None:
            err = f"no CE+PE pair with combined <= {self.s.threshold:g} on {code}"
            if err != self.select_error:
                self.select_error = err
                self.log_line(f"  [{self.name}] {err} — retrying")
            self.status = "no strikes"
            return

        self.key, self.bounds, self.expiry = key, b, code
        self.pair = dict(pick, ce_symbol=chain.symbol_for(pick["ce"], "CE"),
                         pe_symbol=chain.symbol_for(pick["pe"], "PE"),
                         spot=chain.spot, selected_at=self._now_str(now))
        # started on time -> candles from the session start; late -> from the next
        # clean 5-min boundary (a partial first candle would be a fake signal bar)
        self.late_start = now > b[0] + dt.timedelta(minutes=2)
        first = b[0] if not self.late_start else (
            bucket_start(now, self.minutes) + dt.timedelta(minutes=self.minutes))
        self.builder = CandleBuilder(first, self.minutes)
        self.trigger = Trigger(self._enter, self._exit_signal,
                               max_entries=self.s.max_entries,
                               cutoff=self.s.cutoff_dt(b), offset=self.offset,
                               minutes=self.minutes)
        self.status = "running"
        self.audit(now, "cycle_start", expiry=code, ce=pick["ce"], pe=pick["pe"],
                   ce_mark=pick["ce_mark"], pe_mark=pick["pe_mark"],
                   combined=pick["combined"], over_threshold=pick["over_threshold"],
                   distance=pick["distance"], spot=chain.spot, atm=pick["atm"],
                   late_start=self.late_start, first_candle=first.isoformat(),
                   cutoff=self.trigger.cutoff.isoformat(), square_off=b[1].isoformat())
        self.write_state(now)

    def _end_cycle(self, now, get_chain, reason):
        chain = get_chain(self.expiry)
        self.bind(now, chain)
        if self.pos:
            self._try_exit(now, chain, reason, force=True)
        realized = round(sum(t["net_usd"] for t in self.trades), 4)
        summary = {
            "version": self.name, "cycle": self.key, "expiry": self.expiry,
            "ce": self.pair["ce"], "pe": self.pair["pe"],
            "combined_at_select": self.pair["combined"],
            "late_start": self.late_start, "trades": len(self.trades),
            "wins": sum(1 for t in self.trades if t["net_usd"] > 0),
            "fees": round(sum(t["fees"] for t in self.trades), 4),
            "realized": realized, "stop_reason": self.stop_reason,
            "ended": self._now_str(now), "end_reason": reason,
            "unclosed": bool(self.pos),
        }
        self._append(self.results / "cycles.jsonl", summary)
        try:
            (self.charts / f"{self.name}_{self.key.replace(':', '')}.json").write_text(
                json.dumps(self._chart_payload(now), default=str), encoding="utf-8")
        except Exception:
            pass
        self.audit(now, "cycle_end", reason=reason, trades=len(self.trades),
                   realized=realized)
        try:
            self._state_file().unlink(missing_ok=True)
        except Exception:
            pass
        self._reset()
        self.write_state(now)

    # ── sampling + candles ───────────────────────────────────────────────
    def _sample(self, now, ce, pe, chain):
        if ce is None or pe is None:
            return
        comb = ce + pe
        ca = chain.ask.get((float(self.pair["ce"]), "CE"))
        pa = chain.ask.get((float(self.pair["pe"]), "PE"))
        self.last = {"ce": round(ce, 4), "pe": round(pe, 4),
                     "combined": round(comb, 4), "at": now.strftime("%H:%M:%S"),
                     # what buying both legs back would cost RIGHT NOW (best asks)
                     "ce_ask": ca, "pe_ask": pa,
                     "buyback": round(ca + pa, 4) if ca is not None and pa is not None else None}
        closed = self.builder.add(now, comb)
        if closed:
            self._on_close(now, closed, chain)
        self.trigger.on_tick(comb, now)

    def _volume(self, c) -> float:
        """CE + PE contracts traded inside this candle, from Delta's own 5m history.
        A failed fetch counts as 0 volume and is logged — the candle still closes
        on time, because a signal must never wait on a history endpoint."""
        start = _epoch(c["start"])
        total = 0.0
        for sym in (self.pair["ce_symbol"], self.pair["pe_symbol"]):
            try:
                rows = api_candles(sym, f"{self.minutes}m", start, start + self.minutes * 60)
                total += sum(float(r.get("volume") or 0) for r in rows
                             if int(r.get("time", -1)) == start)
            except Exception as e:
                self.log_line(f"  [{self.name}] volume fetch failed {sym}: {e}")
        return total

    def _on_close(self, now, raw, chain):
        c = self.builder.finalize(raw, self._volume(raw))
        pending_before = self.trigger.pending
        self.trigger.on_candle_close(c)
        pend = self.trigger.pending
        if pend != pending_before:
            if pend is None:
                self.audit(now, "trigger_cancel", candle=c["start"].strftime("%H:%M"),
                           close=c["close"], vwap=c["vwap"])
            else:
                self.audit(now, "trigger_armed", candle=c["start"].strftime("%H:%M"),
                           trigger=pend["trigger"], low=c["low"], close=c["close"],
                           vwap=c["vwap"])
        self.write_state(now)

    # ── fills ────────────────────────────────────────────────────────────
    def _fee(self, price, chain):
        qty = self.s.contracts * chain.contract_value
        prem = self.premium_cap * price * qty
        return round(min(self.taker_rate * chain.spot * qty, prem), 6) if chain.spot else round(prem, 6)

    def _fill(self, chain, strike, typ, side):
        f = chain.book_fill(strike, typ, side, self.s.contracts)
        return f if f.get("price") is not None else None

    def _enter(self, trigger, n, reason):
        now = self._clock()
        chain = self._chain_now
        ce = self._fill(chain, self.pair["ce"], "CE", "SELL")
        pe = self._fill(chain, self.pair["pe"], "PE", "SELL")
        if ce is None or pe is None:
            # never half a strangle: unwind whichever leg did fill
            for f, strike, typ in ((ce, self.pair["ce"], "CE"), (pe, self.pair["pe"], "PE")):
                if f is not None:
                    back = self._fill(chain, strike, typ, "BUY")
                    self.audit(now, "entry_unwound", leg=typ, sold=f["price"],
                               bought=back and back["price"])
            self.audit(now, "entry_refused", n=n, reason="no price on a leg")
            return False
        comb = round(ce["price"] + pe["price"], 4)

        # BAD-FILL GUARD. These legs can quote a few hundred contracts at the touch
        # and then gap; a market sell of the full size then sweeps far below the
        # trigger (2026-09-16 00:10: sold 117 against a 150.2 trigger, a 23% round
        # trip, because the CE bid held 119 contracts). A real trader would send a
        # limit through the touch and simply not get filled at that price. So: if the
        # book cannot fill within `max_entry_slippage_pct` of the trigger, sell
        # NOTHING and keep the signal armed for a later tick.
        floor = trigger * (1 - self.max_slip)
        if comb < floor:
            if (now - (self._last_reject or dt.datetime.min)).total_seconds() > 60:
                self._last_reject = now
                self.audit(now, "fill_rejected", n=n, trigger=round(trigger, 2),
                           book_fill=comb, floor=round(floor, 2),
                           ce=ce["price"], pe=pe["price"],
                           reason=f"book fill {comb} is worse than {self.max_slip:.0%} "
                                  f"below the trigger — nothing sold, still armed")
            return "retry"

        fees = self._fee(ce["price"], chain) + self._fee(pe["price"], chain)
        self.pos = {
            "n": n, "signal": reason, "trigger": trigger,
            "entry_time": self._now_str(now), "ce_sell": ce["price"], "pe_sell": pe["price"],
            "entry_combined": comb, "entry_slippage": round(comb - trigger, 4),
            "entry_fees": round(fees, 6), "entry_src": f"{ce.get('source')}/{pe.get('source')}",
            "ce_exit": None, "pe_exit": None, "exit_pending": None,
        }
        self.audit(now, "ENTRY", n=n, trigger=trigger, ce=ce["price"], pe=pe["price"],
                   combined=comb, src=self.pos["entry_src"])
        self.write_state(now)
        return True

    def _exit_signal(self, n, reason):
        return self._try_exit(self._clock(), self._chain_now, reason)

    def _try_exit(self, now, chain, reason, force=False):
        """Buy back both legs. A leg that cannot be priced stays open and is retried
        next poll. Returns True once the position is fully closed."""
        p = self.pos
        if p is None:
            return True
        if p.get("exit_signal_close") is None and self.builder and self.builder.closed:
            p["exit_signal_close"] = self.builder.closed[-1]["close"]
        for typ, strike in (("CE", self.pair["ce"]), ("PE", self.pair["pe"])):
            k = "ce_exit" if typ == "CE" else "pe_exit"
            if p[k] is None and chain is not None:
                f = self._fill(chain, strike, typ, "BUY")
                if f is not None:
                    p[k] = f["price"]
                    p[k + "_src"] = f.get("source")
                    p[k + "_fee"] = self._fee(f["price"], chain)
        if p["ce_exit"] is None or p["pe_exit"] is None:
            p["exit_pending"] = reason
            self.audit(now, "exit_retry", reason=reason, ce=p["ce_exit"], pe=p["pe_exit"])
            return False
        cv = chain.contract_value if chain else 0.001
        exit_comb = round(p["ce_exit"] + p["pe_exit"], 4)
        points = round(p["entry_combined"] - exit_comb, 4)
        gross = round(points * self.s.contracts * cv, 4)
        fees = round(p["entry_fees"] + p["ce_exit_fee"] + p["pe_exit_fee"], 6)
        t = dict(p, exit_time=self._now_str(now), exit_reason=reason,
                 exit_combined=exit_comb, points=points, gross_usd=gross,
                 fees=round(fees, 4), net_usd=round(gross - fees, 4),
                 version=self.name, cycle=self.key, expiry=self.expiry,
                 ce_strike=self.pair["ce"], pe_strike=self.pair["pe"],
                 contracts=self.s.contracts)
        t.pop("exit_pending", None)
        self.trades.append(t)
        self._append(self.results / "trades.jsonl", t)
        self.pos = None
        if self.trigger:
            self.trigger.in_pos = False
        self.audit(now, "EXIT", n=t["n"], reason=reason, combined=exit_comb,
                   points=points, net_usd=t["net_usd"])
        self.write_state(now)
        return True

    # ── risk ─────────────────────────────────────────────────────────────
    def realized(self) -> float:
        return round(sum(t["net_usd"] for t in self.trades), 4)

    def unrealized(self, chain=None) -> float:
        """Open P&L as a broker would show a short you could close NOW: the real
        sell fills minus the cost to buy both legs back at the best asks (falls back
        to the mark only if a side of the book is empty), less the entry fees."""
        p = self.pos
        if not p:
            return 0.0
        exit_px = self.last.get("buyback")
        if exit_px is None:
            exit_px = self.last.get("combined")
        if exit_px is None:
            return 0.0
        cv = chain.contract_value if chain else 0.001
        return round((p["entry_combined"] - exit_px) * self.s.contracts * cv
                     - p["entry_fees"], 4)

    def mtm(self, chain=None) -> float:
        return round(self.realized() + self.unrealized(chain), 4)

    def _check_mtm_stop(self, now, chain):
        if not self.s.mtm_stop or self.trigger is None or self.trigger.done:
            return
        m = self.mtm(chain)
        if m <= -self.s.mtm_stop:
            self.stop_reason = f"MTM stop ${m:+.2f} <= -${self.s.mtm_stop:g}"
            self.trigger.done = True
            self.trigger.pending = None
            self.status = "stopped (MTM)"
            self.audit(now, "MTM_STOP", mtm=m)
            if self.pos:
                self._try_exit(now, chain, "MTM stop")

    # ── clock/chain handed to trigger callbacks ──────────────────────────
    _clock = staticmethod(lambda: dt.datetime.now())
    _chain_now = None

    def bind(self, now, chain):
        """Called by the engine before any step, so the trigger's callbacks price
        against the right chain and stamp the strategy clock."""
        self._clock = lambda: now
        self._chain_now = chain

    # ── persistence ──────────────────────────────────────────────────────
    def _state_file(self) -> Path:
        return self.state_dir / f"{self.name}_resume.json"

    def _append(self, path: Path, rec: dict):
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception:
            pass

    def _restore(self, now, key) -> bool:
        f = self._state_file()
        if not f.exists():
            return False
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return False
        if d.get("key") != key:
            return False
        self.key = key
        self.bounds = self.s.bounds(now)
        self.expiry = d["expiry"]
        self.pair = d["pair"]
        self.late_start = d.get("late_start", False)
        self.builder = CandleBuilder.from_dict(d["builder"])
        # a candle that was forming when the engine died is incomplete — drop it
        self.builder.cur = None
        self.trigger = Trigger(self._enter, self._exit_signal,
                               max_entries=self.s.max_entries,
                               cutoff=self.s.cutoff_dt(self.bounds), offset=self.offset,
                               minutes=self.minutes)
        self.trigger.load(d["trigger"])
        self.pos = d.get("pos")
        self.trades = d.get("trades") or []
        self.stop_reason = d.get("stop_reason")
        self.trigger.in_pos = self.pos is not None
        self.status = "running" if not self.trigger.done else "done"
        self.audit(now, "resumed", candles=len(self.builder.closed),
                   in_pos=self.pos is not None, trades=len(self.trades))
        return True

    def _save_resume(self):
        if not self.active:
            return
        d = {"key": self.key, "expiry": self.expiry, "pair": self.pair,
             "late_start": self.late_start, "builder": self.builder.to_dict(),
             "trigger": self.trigger.to_dict(), "pos": self.pos, "trades": self.trades,
             "stop_reason": self.stop_reason}
        atomic_write(self._state_file(), json.dumps(d, default=str))

    def _record_equity(self, now):
        m = now.strftime("%Y-%m-%d %H:%M")
        if m == self._last_equity_min or not self.active:
            return
        self._last_equity_min = m
        self._append(self.results / f"{self.name}_equity.jsonl",
                     {"ts": m, "cycle": self.key, "mtm": self.mtm(),
                      "realized": self.realized(), "combined": self.last["combined"],
                      "buyback": self.last.get("buyback"),
                      "in_pos": self.pos is not None})

    # ── snapshot for the dashboard ───────────────────────────────────────
    def _chart_payload(self, now) -> dict:
        ser = lambda c: {"t": c["start"].strftime("%Y-%m-%d %H:%M"), "o": c["open"],
                         "h": c["high"], "l": c["low"], "c": c["close"],
                         "v": c.get("volume"), "vwap": c.get("vwap")}
        b = self.builder
        return {
            "version": self.name, "label": self.s.label, "cycle": self.key,
            "expiry": self.expiry, "pair": self.pair, "late_start": self.late_start,
            "candles": [ser(c) for c in b.closed] if b else [],
            "forming": ({"t": b.cur["start"].strftime("%Y-%m-%d %H:%M"), "o": b.cur["open"],
                         "h": b.cur["high"], "l": b.cur["low"], "c": b.cur["close"]}
                        if b and b.cur else None),
            "trades": self.trades, "position": self.pos,
            "stop_reason": self.stop_reason,
            "params": {"threshold": self.s.threshold, "contracts": self.s.contracts,
                       "max_entries": self.s.max_entries, "mtm_stop_usd": self.s.mtm_stop,
                       "fallback_last_pair": self.s.fallback_last,
                       "start": f"{self.s.start:%H:%M}", "cutoff": f"{self.s.cutoff:%H:%M}",
                       "square_off": f"{self.s.square_off:%H:%M}"},
            "updated": self._now_str(now),
        }

    def snapshot(self, now) -> dict:
        d = self._chart_payload(now)
        tr = self.trigger
        d.update({
            "status": self.status, "stop_reason": self.stop_reason,
            "select_error": self.select_error if not self.active else None,
            "bounds": [x.isoformat() for x in self.bounds] if self.bounds else None,
            "entries": tr.entries if tr else 0,
            "pending": tr.pending if tr else None,
            "in_pos": self.pos is not None,
            "done": tr.done if tr else False,
            "last": self.last, "vwap": self.builder.live_vwap() if self.builder else None,
            "realized": self.realized(), "mtm": self.mtm(),
        })
        return d

    def write_state(self, now):
        try:
            self._save_resume()
        except Exception as e:
            self.log_line(f"  [{self.name}] resume save failed: {e}")
        try:
            atomic_write(self.state_dir / f"{self.name}.json",
                         json.dumps(self.snapshot(now), default=str))
        except Exception:
            pass
