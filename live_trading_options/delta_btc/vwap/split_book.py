"""
vwap/split_book.py — the split-leg ("D") version of the BTC VWAP strangle, paper or LIVE.
========================================================================================

Same entries, candles and strikes as VwapBook. What changes is the exit:

  BUSTED LEG   a combined candle closing above VWAP buys back ONLY the leg that rose
               most since it was sold (mark now - sell price). The other leg is KEPT.
  KEPT LEG     builds its own 5-min candles + VWAP (its own mark, its own traded
               volume, from the session start). While it is alone, its own candle
               closing above its own VWAP buys it back.
  RE-ENTRY     the next entry signal sells only the leg that is missing. Once both
               legs are on again it is a normal strangle (the kept-leg rule stops).
  STOP         the day's PRICE P&L (sell - buy, open legs at the mark, no fees, no
               spread) at or below -mtm_stop_usd closes everything for the day.
  KILL         from the control file: close everything, no more entries today.

Validated before it was built: the replay of the 16 live paper days (2026-10-03)
and the 2026 history (backtest/vwap_split_2026.py).

Paper vs live is decided by data/vwap_state/live_control_{version}.json:
    {"mode": "paper"|"live", "kill": false}
Mode changes take effect only while flat. Live needs DELTA_API_KEY/SECRET in
deployment/.env (see live/delta_client.py). Every live order goes through
vwap/leg_exec.LiveExec.

Safety rules:
  - a leg stays OPEN in the book until Delta confirms it is bought back; a failed or
    partial exit is retried every poll, including past square-off
  - a half-filled entry is unwound at once — never a lone leg nobody chose
  - a BUY never exceeds the short Delta shows (never opens a long)
  - an insufficient-margin refusal stops entries for the day
  - a restart rebuilds the legs from the resume file and checks them against Delta
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import time
import datetime as dt
from pathlib import Path

from core.api import candles as api_candles
from vwap.book import VwapBook, atomic_write, _epoch
from vwap.strategy import CandleBuilder, Trigger
from vwap.leg_exec import PaperExec, LiveExec
import vwap.leg_exec as LE

LEGS = ("CE", "PE")


class SplitVwapBook(VwapBook):
    def __init__(self, name, params, root, log=print):
        super().__init__(name, params, root, log=log)
        cfg = params["versions"][name]
        self.live_cfg = params.get("live_orders") or {}
        self.stop_basis = cfg.get("stop_basis", "price")
        # only a live_capable version can ever place a real order; the others are
        # paper references that ignore an arm request
        self.live_capable = bool(cfg.get("live_capable", False))
        # optional hard cap: combined premium >= cap x VWAP (any tick, not waiting
        # for the candle close) splits at once — the "1.5x" paper version
        self.vwap_cap = float(cfg.get("vwap_cap_mult") or 0)
        self.gst_rate = float((params.get("fees") or {}).get("gst_rate", 0.18))
        self.control_file = self.state_dir / f"live_control_{name}.json"
        self.mode = "paper"
        self.exec = PaperExec(self.s.contracts)
        self.margin_halt = False
        self._ctl_at = 0.0
        self._client = None

    # ── state ────────────────────────────────────────────────────────────
    def _reset(self):
        super()._reset()
        self.legs = {"CE": None, "PE": None}
        self.leg_builders = None
        self._leg_closed = {}

    def _open(self):
        return [t for t in LEGS if self.legs.get(t)]

    def _sync_pos(self):
        """`self.pos` is what the base class, the engine and the dashboard read."""
        op = self._open()
        if not op:
            self.pos = None
            return
        L = {t: self.legs[t] for t in op}
        self.pos = {
            "n": max(l["n"] for l in L.values()),
            "legs": L,
            "entry_combined": round(sum(l["sell"] for l in L.values()), 4),
            "entry_fees": round(sum(l.get("sell_fee") or 0 for l in L.values()), 6),
            "entry_time": min(l["sell_time"] for l in L.values()),
            "kept": next((t for t in op if self.legs[t].get("kept")), None),
            "exit_pending": next((self.legs[t]["exit_pending"] for t in op
                                  if self.legs[t].get("exit_pending")), None),
            "mode": self.mode,
        }

    # ── control: paper / live / kill ─────────────────────────────────────
    def _read_control(self):
        d = {"mode": "paper", "kill": False}
        try:
            if self.control_file.exists():
                d.update(json.loads(self.control_file.read_text(encoding="utf-8")))
        except Exception:
            pass
        # a KILL pressed on an earlier day does not carry into today
        if d.get("kill") and str(d.get("updated") or "")[:10] != dt.date.today().isoformat():
            d["kill"] = False
        return d

    def _apply_control(self, now, chain):
        if time.monotonic() - self._ctl_at < 3:
            return
        self._ctl_at = time.monotonic()
        d = self._read_control()
        want = "live" if (d.get("mode") == "live" and self.live_capable) else "paper"
        if want != self.mode and not self._open():
            if want == "live":
                try:
                    from live.delta_client import DeltaClient
                    self._client = self._client or DeltaClient()
                    self.exec = LiveExec(self._client, self.live_cfg, log=self.log_line)
                    self.mode = "live"
                    self.audit(now, "MODE", mode="live")
                except Exception as e:
                    if getattr(self, "_mode_err", None) != str(e):
                        self._mode_err = str(e)
                        self.audit(now, "MODE_REFUSED", wanted="live", error=str(e))
            else:
                self.exec = PaperExec(self.s.contracts)
                self.mode = "paper"
                self.audit(now, "MODE", mode="paper")
        if d.get("kill") and self.trigger is not None and not self.trigger.done:
            self.stop_reason = "KILL pressed"
            self.trigger.done = True
            self.trigger.pending = None
            self.status = "killed"
            self.audit(now, "KILL")
            self._close_all(now, chain, "KILL")

    # ── engine entry point ───────────────────────────────────────────────
    def on_poll(self, now, get_chain):
        b = self.s.bounds(now)
        if self.active and (b is None or self.s.key(b) != self.key):
            self._end_cycle(now, get_chain, "square-off")
            if self.active:               # a live leg is still open: keep retrying
                return
        if b is None:
            self.status = "waiting"
            if not self.active:
                self._apply_control(now, None)
            return
        if not self.active:
            self._apply_control(now, None)
            self._start_cycle(now, b, get_chain)
            if not self.active:
                return
        chain = get_chain(self.expiry)
        if chain is None:
            return
        self.bind(now, chain)
        self._apply_control(now, chain)

        ce = chain.mark.get((self.pair["ce"], "CE"))
        pe = chain.mark.get((self.pair["pe"], "PE"))
        self._sample(now, ce, pe, chain)
        closed = self.builder.due(now)
        if closed:
            self._on_close(now, closed, chain)

        for t in self._open():
            if self.legs[t].get("exit_pending"):
                self._close_leg(now, chain, t, self.legs[t]["exit_pending"])

        self._check_mtm_stop(now, chain)
        self._record_equity(now)
        self.write_state(now)

    # ── cycle ────────────────────────────────────────────────────────────
    def _start_cycle(self, now, b, get_chain):
        super()._start_cycle(now, b, get_chain)
        if self.active and self.leg_builders is None:
            self.leg_builders = {t: CandleBuilder(self.builder.first, self.minutes) for t in LEGS}
            self.write_state(now)

    def _end_cycle(self, now, get_chain, reason):
        chain = get_chain(self.expiry)
        self.bind(now, chain)
        if self._open():
            self._close_all(now, chain, reason)
            if self._open():
                self.status = "SQUARE-OFF PENDING"
                self.audit(now, "SQUAREOFF_PENDING", legs=self._open())
                self.write_state(now)
                return
        realized = self.realized()
        summary = {
            "version": self.name, "cycle": self.key, "expiry": self.expiry, "mode": self.mode,
            "ce": self.pair["ce"], "pe": self.pair["pe"],
            "combined_at_select": self.pair["combined"], "late_start": self.late_start,
            "entries": self.trigger.entries if self.trigger else 0,
            "leg_trades": len(self.trades),
            "fees": round(sum(t["fees"] for t in self.trades), 4),
            "price_pnl": self.price_realized(), "realized": realized,
            "stop_reason": self.stop_reason, "ended": self._now_str(now), "end_reason": reason,
        }
        self._append(self.results / "cycles.jsonl", summary)
        try:
            (self.charts / f"{self.name}_{self.key.replace(':', '')}.json").write_text(
                json.dumps(self._chart_payload(now), default=str), encoding="utf-8")
        except Exception:
            pass
        self.audit(now, "cycle_end", reason=reason, entries=summary["entries"],
                   price_pnl=summary["price_pnl"], realized=realized)
        try:
            self._state_file().unlink(missing_ok=True)
        except Exception:
            pass
        mode, ex, client = self.mode, self.exec, self._client
        self._reset()
        self.mode, self.exec, self._client = mode, ex, client
        self.margin_halt = False
        self.write_state(now)

    # ── sampling + candles ───────────────────────────────────────────────
    def _sample(self, now, ce, pe, chain):
        if ce is None or pe is None:
            return
        if self.leg_builders:
            for t, v in (("CE", ce), ("PE", pe)):
                c = self.leg_builders[t].add(now, v)
                if c:
                    self._leg_closed[t] = c
        super()._sample(now, ce, pe, chain)
        self._check_vwap_cap(now, ce + pe)

    def _check_vwap_cap(self, now, comb):
        """VWAP CAP (vwap_cap_mult, e.g. 1.5): with BOTH legs on, the combined premium
        trading at or above cap x the current VWAP buys back the busted leg right
        away — the same split as a close above VWAP, only without waiting for the
        5-min candle to finish."""
        if not self.vwap_cap or self.trigger is None or self.trigger.done:
            return
        if len(self._open()) != 2 or not self.trigger.in_pos:
            return
        if any(self.legs[t].get("exit_pending") for t in LEGS):
            return
        vwap = self.builder.live_vwap() if self.builder else None
        if not vwap or comb < self.vwap_cap * vwap:
            return
        self.audit(now, "VWAP_CAP", combined=round(comb, 4), vwap=vwap,
                   level=round(self.vwap_cap * vwap, 4))
        self._exit_signal(self.trigger.entries, f"combined {comb:.1f} >= {self.vwap_cap:g}x VWAP {vwap:.1f}")
        self.trigger.in_pos = False

    def _leg_volumes(self, c):
        start = _epoch(c["start"])
        out = {}
        for t, sym in (("CE", self.pair["ce_symbol"]), ("PE", self.pair["pe_symbol"])):
            try:
                rows = api_candles(sym, f"{self.minutes}m", start, start + self.minutes * 60)
                out[t] = sum(float(r.get("volume") or 0) for r in rows
                             if int(r.get("time", -1)) == start)
            except Exception as e:
                out[t] = 0.0
                self.log_line(f"  [{self.name}] volume fetch failed {sym}: {e}")
        return out

    def _on_close(self, now, raw, chain):
        vols = self._leg_volumes(raw)
        c = self.builder.finalize(raw, vols["CE"] + vols["PE"])
        # the legs' own candles for the same 5 minutes
        leg_c = {}
        if self.leg_builders:
            for t in LEGS:
                lb = self.leg_builders[t]
                lr = self._leg_closed.pop(t, None)
                if lr is None and lb.cur is not None and lb.cur["start"] <= raw["start"]:
                    lr = lb._close()
                if lr is not None:
                    leg_c[t] = lb.finalize(lr, vols[t])

        # KEPT LEG: alone, its own close above its own VWAP buys it back. Checked
        # BEFORE the combined signal, so a leg kept at this very close is not judged
        # on the candle it was kept in.
        op = self._open()
        if len(op) == 1 and self.legs[op[0]].get("kept") and op[0] in leg_c:
            t = op[0]
            lc = leg_c[t]
            if lc["start"] >= self.legs[t]["split_candle"] and lc["close"] > lc["vwap"]:
                self.audit(now, "KEPT_LEG_EXIT", leg=t, close=lc["close"], vwap=lc["vwap"])
                self._close_leg(now, chain, t, f"{t} own close above own VWAP")

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

    # ── fees / marks ─────────────────────────────────────────────────────
    def _fee_q(self, price, qty, chain):
        """Delta's fee for one side of one leg INCLUDING 18% GST — what the wallet is
        actually debited (verified in Delta's ledger 2026-10-06: fee 0.875 + GST
        0.1575 = 1.0325). Live trades book Delta's own commission (already incl. GST);
        this model is for paper fills and for estimating the exit of an open leg."""
        chain = chain or self._chain_now
        q = qty * (chain.contract_value if chain else 0.001)
        prem = self.premium_cap * price * q
        spot = chain.spot if chain else None
        fee = min(self.taker_rate * spot * q, prem) if spot else prem
        return round(fee * (1 + self.gst_rate), 6)

    def _mark(self, t):
        return self.last.get("ce" if t == "CE" else "pe")

    def _strike(self, t):
        return self.pair["ce" if t == "CE" else "pe"]

    # ── entry ────────────────────────────────────────────────────────────
    def _enter(self, trigger, n, reason):
        now, chain = self._clock(), self._chain_now
        if self.margin_halt:
            return False
        if any(self.legs[t] and self.legs[t].get("exit_pending") for t in LEGS):
            return "retry"                      # a leg is still being bought back
        need = [t for t in LEGS if not self.legs[t]]
        if not need:
            return False
        floor_comb = trigger * (1 - self.max_slip)
        kept_val = sum(self._mark(t) or 0 for t in LEGS if self.legs[t])
        marks = {t: self._mark(t) or 0 for t in need}
        msum = sum(marks.values()) or 1.0
        floors = {t: max(0.0, (floor_comb - kept_val) * marks[t] / msum) for t in need}

        if not self.exec.live:
            fills = {t: self.exec.execute(chain, self._strike(t), t, "sell", self._target_qty(),
                                          "entry") for t in need}
            if any(not f["ok"] for f in fills.values()):
                self.audit(now, "entry_refused", n=n, reason="no price on a leg")
                return False
            got = sum(f["price"] for f in fills.values()) + kept_val
            if got < floor_comb:
                if (now - (self._last_reject or dt.datetime.min)).total_seconds() > 60:
                    self._last_reject = now
                    self.audit(now, "fill_rejected", n=n, trigger=round(trigger, 2),
                               book_fill=round(got, 2), floor=round(floor_comb, 2),
                               reason="book fill worse than the slippage guard — still armed")
                return "retry"
        else:
            # BOTH LEGS AT THE SAME MOMENT (two threads), so the market cannot move
            # between them. Then:
            #   both fully filled                -> done
            #   both filled, one only partly     -> trim the bigger leg down to the
            #                                       smaller (equal, still a strangle)
            #   one leg nothing / under half     -> buy back whatever sold: flat
            # A lone leg nobody chose is never kept.
            target = self._target_qty()
            # COMBINED PRE-CHECK on the real books BEFORE any order (fix 2026-10-06).
            # The guard used to be per leg (the combined floor split by marks), so a
            # leg whose bid sat a few cents under its share was not sent while the
            # other leg sold — then unwound at a loss, burning an entry. 04-Oct lost
            # 1 entry, 06-Oct all 4 (−$12.82), while paper (combined guard) entered.
            # Now: walk both books for the full size; if the COMBINED sweep (+ any
            # kept leg's mark) is under the floor, send NOTHING and stay armed.
            # Otherwise each leg's own floor is its sweep minus its share of the
            # combined headroom, so together they can never sell below the floor.
            sweeps = {}
            for t in need:
                c = chain.contract(self._strike(t), t) or {}
                try:
                    bk = LE.orderbook(c.get("symbol"))
                    sweeps[t] = LE._sweep_price(bk.get("buy"), target)[0]
                except Exception:
                    sweeps[t] = None
            if any(v is None for v in sweeps.values()) or \
                    sum(sweeps.values()) + kept_val < floor_comb:
                if (now - (self._last_reject or dt.datetime.min)).total_seconds() > 60:
                    self._last_reject = now
                    self.audit(now, "fill_rejected", n=n, trigger=round(trigger, 2),
                               book={t: v for t, v in sweeps.items()},
                               kept_val=round(kept_val, 2), floor=round(floor_comb, 2),
                               reason="combined book below the slippage floor — nothing sent, still armed")
                return "retry"
            headroom = sum(sweeps.values()) + kept_val - floor_comb
            ssum = sum(sweeps.values()) or 1.0
            floors = {t: max(0.0, sweeps[t] - headroom * sweeps[t] / ssum) for t in need}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(need)) as ex:
                futs = {t: ex.submit(self.exec.execute, chain, self._strike(t), t, "sell",
                                     target, f"entry#{n}", floor=floors[t], tag=f"{t}{n}")
                        for t in need}
                fills = {t: f.result() for t, f in futs.items()}
            margin = any(f.get("margin") for f in fills.values())
            got = {t: int(f["filled"]) for t, f in fills.items()}
            least = min(got.values())
            usable = least >= max(1, target // 2) and not margin
            if not usable:
                sold = {t: f for t, f in fills.items() if f["filled"]}
                for t, f in sold.items():
                    self.audit(now, "entry_unwind", leg=t, filled=f["filled"], sold=f["price"])
                    self._unwind(now, chain, t, int(f["filled"]), f, n, trigger)
                if margin:
                    self.margin_halt = True
                    self.trigger.done = True
                    self.stop_reason = "insufficient margin — no more entries today"
                    self.status = "margin halt"
                self.audit(now, "entry_failed", n=n, filled=got, margin=margin,
                           errors={t: f.get("error") for t, f in fills.items()})
                if not sold and not margin:
                    return "retry"                # nothing sold: the signal stays armed
                return False
            for t, f in fills.items():
                extra = int(f["filled"]) - least
                if extra > 0:                     # part-fill on the other leg: equalize
                    self.audit(now, "entry_equalize", leg=t, filled=f["filled"], keep=least)
                    left = self._unwind(now, chain, t, extra, dict(f), n, trigger, register=False)
                    # the trimmed part's sell fee went with its unwind trade
                    f["fee"] = (f.get("fee") or 0) * least / max(int(f["filled"]), 1)
                    f["filled"] = least + left   # anything not bought back stays ours

        for t, f in fills.items():
            qty = int(f["filled"]) if self.exec.live else self._target_qty()
            fee = f["fee"] if f.get("fee") is not None else self._fee_q(f["price"], qty, chain)
            c = chain.contract(self._strike(t), t) or {}
            mk = marks.get(t) or None
            self.legs[t] = {"typ": t, "strike": self._strike(t), "symbol": c.get("symbol"),
                            "product_id": c.get("product_id"),
                            "qty": qty, "sell": round(f["price"], 4),
                            "sell_time": self._now_str(now), "sell_fee": round(fee, 6),
                            "n": n, "trigger": trigger, "kept": False, "split_candle": None,
                            "exit_pending": None, "mode": self.mode,
                            # execution quality: the mark the decision was taken at,
                            # and what the fill gave up against it (+ = better than mark)
                            "entry_mark": round(mk, 4) if mk else None,
                            "entry_slip": round(f["price"] - mk, 4) if mk else None,
                            "entry_ms": f.get("ms"),
                            "orders": f.get("orders")}
        for t in LEGS:                         # a pair again: the kept-leg rule stops
            if self.legs[t]:
                self.legs[t]["kept"] = False
        self._sync_pos()
        self.audit(now, "ENTRY", n=n, trigger=trigger, sold={t: fills[t]["price"] for t in fills},
                   kept=[t for t in LEGS if t not in fills], mode=self.mode,
                   combined=self.pos["entry_combined"])
        self.write_state(now)
        return True

    def _target_qty(self):
        """Contracts per leg for an entry: a re-sold leg matches the kept leg's size
        (it may have been trimmed by a part-fill), a fresh strangle uses the config."""
        op = self._open()
        return int(self.legs[op[0]]["qty"]) if op else self.s.contracts

    def _unwind(self, now, chain, t, qty, sold, n, trigger, register=True):
        """Buy back `qty` contracts of leg `t` that were just sold at `sold['price']`
        (a failed or lopsided entry). Must complete; the round trip is booked as a
        trade so the P&L, the stop and the table stay honest. Returns contracts it
        could NOT buy back. With register=True those stay in the book as an open leg
        whose exit is retried every poll."""
        want, got_cost, got_fee, orders = qty, 0.0, 0.0, []
        for _ in range(5):
            r = self.exec.execute(chain, self._strike(t), t, "buy", qty, "unwind", tag=f"U{t}")
            orders += r.get("orders") or []
            if r["filled"] and r["price"] is not None:
                got_cost += r["filled"] * r["price"]
                got_fee += r.get("fee") or 0
            qty -= int(r["filled"])
            if r["ok"] or r.get("broker_flat") or qty <= 0:
                qty = max(0, qty) if not r.get("broker_flat") else 0
                break
        bought = want - qty
        if bought > 0 and got_cost:
            share = (sold.get("fee") or 0) * bought / max(int(sold["filled"]), 1)
            leg = {"typ": t, "n": n, "trigger": trigger, "sell": sold["price"],
                   "sell_time": self._now_str(now), "sell_fee": share, "qty": bought,
                   "kept": False, "orders": sold.get("orders")}
            self._record_leg_trade(now, chain, leg, bought, round(got_cost / bought, 6),
                                   got_fee, "unwind (entry not completed)", {"orders": orders})
        if qty > 0:
            self.audit(now, "UNWIND_INCOMPLETE", leg=t, left=qty)
            if register:
                c = chain.contract(self._strike(t), t) or {}
                self.legs[t] = {"typ": t, "strike": self._strike(t), "symbol": c.get("symbol"),
                                "product_id": c.get("product_id"), "qty": qty,
                                "sell": sold["price"], "sell_time": self._now_str(now),
                                "sell_fee": 0, "n": n, "kept": False, "split_candle": None,
                                "exit_pending": "unwind", "mode": self.mode}
                self._sync_pos()
        return qty

    # ── exits ────────────────────────────────────────────────────────────
    def _exit_signal(self, n, reason):
        now, chain = self._clock(), self._chain_now
        op = self._open()
        if len(op) == 2:
            rise = {t: (self._mark(t) or self.legs[t]["sell"]) - self.legs[t]["sell"] for t in op}
            bust = max(rise, key=rise.get)
            keep = "PE" if bust == "CE" else "CE"
            self._close_leg(now, chain, bust, f"busted leg ({reason})")
            if self.legs[keep]:
                self.legs[keep]["kept"] = True
                # its own candles are judged from the NEXT one on
                self.legs[keep]["split_candle"] = self.builder.closed[-1]["start"] + \
                    dt.timedelta(minutes=self.minutes) if self.builder.closed else now
                self.audit(now, "SPLIT", busted=bust, kept=keep,
                           rise={k: round(v, 2) for k, v in rise.items()})
            self._sync_pos()
            return True
        for t in op:
            self._close_leg(now, chain, t, reason)
        return True

    def _close_all(self, now, chain, reason):
        for t in self._open():
            self._close_leg(now, chain, t, reason)

    def _try_exit(self, now, chain, reason, force=False):
        self._close_all(now, chain, reason)
        return not self._open()

    def _close_leg(self, now, chain, t, reason):
        leg = self.legs[t]
        if leg is None:
            return True
        if chain is None:
            leg["exit_pending"] = reason
            self._sync_pos()
            return False
        r = self.exec.execute(chain, leg["strike"], t, "buy", leg["qty"], reason, tag=f"X{t}")
        filled = int(r["filled"]) if self.exec.live else leg["qty"]
        price = r["price"]
        if r.get("broker_flat"):
            # Delta shows no short to buy back (expired, or closed by hand): book it at
            # the mark and say so loudly
            price, filled = (self._mark(t) or 0.0), leg["qty"]
            self.audit(now, "BROKER_FLAT", leg=t, qty=leg["qty"], booked_at_mark=price)
        if filled and price is not None:
            fee = r["fee"] if (self.exec.live and r.get("fee") is not None) else \
                self._fee_q(price, filled, chain)
            self._record_leg_trade(now, chain, leg, filled, price, fee, reason, r)
            leg["qty"] -= filled
            leg["sell_fee"] = round((leg.get("sell_fee") or 0) * (leg["qty"] / (leg["qty"] + filled)), 6)
        if leg["qty"] <= 0:
            self.legs[t] = None
            self._sync_pos()
            if self.trigger and not self._open():
                self.trigger.in_pos = False
            self.write_state(now)
            return True
        leg["exit_pending"] = reason
        self._sync_pos()
        self.audit(now, "exit_retry", leg=t, left=leg["qty"], reason=reason,
                   error=r.get("error"))
        return False

    def _record_leg_trade(self, now, chain, leg, qty, price, fee, reason, r):
        cv = chain.contract_value if chain else 0.001
        t = leg["typ"]
        sell_fee = (leg.get("sell_fee") or 0) * qty / max(leg["qty"], 1)
        points = round(leg["sell"] - price, 4)
        gross = round(points * qty * cv, 4)
        fees = round(sell_fee + fee, 6)
        mk = self._mark(t)
        rec = {"n": leg["n"], "leg": t, "mode": self.mode,
               "entry_mark": leg.get("entry_mark"), "entry_slippage": leg.get("entry_slip"),
               "exit_mark": round(mk, 4) if mk else None,
               "exit_slippage": round(mk - price, 4) if mk else None,
               "entry_ms": leg.get("entry_ms"), "exit_ms": r.get("ms"),
               "entry_time": leg["sell_time"], "trigger": leg.get("trigger"),
               "ce_sell": leg["sell"] if t == "CE" else None,
               "pe_sell": leg["sell"] if t == "PE" else None,
               "entry_combined": leg["sell"], "entry_fees": round(sell_fee, 6),
               "exit_time": self._now_str(now), "exit_reason": reason,
               "ce_exit": price if t == "CE" else None, "pe_exit": price if t == "PE" else None,
               "exit_combined": price, "points": points, "gross_usd": gross,
               "fees": round(fees, 4), "net_usd": round(gross - fees, 4),
               "version": self.name, "cycle": self.key, "expiry": self.expiry,
               "ce_strike": self.pair["ce"], "pe_strike": self.pair["pe"],
               "contracts": qty, "kept": bool(leg.get("kept")),
               "orders": (r.get("orders") or []) + (leg.get("orders") or [])}
        self.trades.append(rec)
        self._append(self.results / "trades.jsonl", rec)
        self.audit(now, "EXIT", leg=t, n=leg["n"], reason=reason, sold=leg["sell"],
                   bought=price, qty=qty, gross_usd=gross, net_usd=rec["net_usd"], mode=self.mode)

    # ── P&L + the stop ───────────────────────────────────────────────────
    def price_realized(self) -> float:
        return round(sum(t["gross_usd"] for t in self.trades), 4)

    def price_open(self, chain=None) -> float:
        cv = chain.contract_value if chain else 0.001
        s = 0.0
        for t in self._open():
            l, m = self.legs[t], self._mark(t)
            if m is not None:
                s += (l["sell"] - m) * l["qty"] * cv
        return round(s, 4)

    def unrealized(self, chain=None) -> float:
        """Net view of the open legs, ALL charges: what buying them back costs now
        (best ask), less the entry fee already paid and the estimated exit fee,
        both incl. GST. realized() + unrealized() = net P&L if closed this instant."""
        cv = chain.contract_value if chain else 0.001
        s = 0.0
        for t in self._open():
            l = self.legs[t]
            ask = self.last.get("ce_ask" if t == "CE" else "pe_ask") or self._mark(t)
            if ask is not None:
                s += ((l["sell"] - ask) * l["qty"] * cv - (l.get("sell_fee") or 0)
                      - self._fee_q(ask, l["qty"], chain))
        return round(s, 4)

    def charges(self) -> float:
        """Fees + GST paid today (closed leg trades + entry fees of open legs)."""
        return round(sum(t["fees"] for t in self.trades)
                     + sum(self.legs[t].get("sell_fee") or 0 for t in self._open()), 4)

    def price_mtm(self, chain=None) -> float:
        return round(self.price_realized() + self.price_open(chain), 4)

    def mtm(self, chain=None) -> float:
        """What the stop watches: price P&L only (user rule 2026-10-03)."""
        if self.stop_basis == "price":
            return self.price_mtm(chain)
        return round(self.realized() + self.unrealized(chain), 4)

    def _check_mtm_stop(self, now, chain):
        if not self.s.mtm_stop or self.trigger is None:
            return
        if self.trigger.done and not self._open():
            return
        m = self.mtm(chain)
        if m <= -self.s.mtm_stop and not self.stop_reason:
            self.stop_reason = f"MTM stop ${m:+.2f} <= -${self.s.mtm_stop:g}"
            self.trigger.done = True
            self.trigger.pending = None
            self.status = "stopped (MTM)"
            self.audit(now, "MTM_STOP", mtm=m)
            self._close_all(now, chain, "MTM stop")

    # ── persistence ──────────────────────────────────────────────────────
    def _save_resume(self):
        if not self.active:
            return
        ser = lambda l: dict(l, split_candle=l["split_candle"].isoformat()
                             if isinstance(l.get("split_candle"), dt.datetime) else l.get("split_candle"))
        d = {"key": self.key, "expiry": self.expiry, "pair": self.pair,
             "late_start": self.late_start, "builder": self.builder.to_dict(),
             "trigger": self.trigger.to_dict(), "pos": self.pos, "trades": self.trades,
             "stop_reason": self.stop_reason, "mode": self.mode, "margin_halt": self.margin_halt,
             "legs": {t: (ser(l) if l else None) for t, l in self.legs.items()},
             "leg_builders": {t: b.to_dict() for t, b in (self.leg_builders or {}).items()}}
        atomic_write(self._state_file(), json.dumps(d, default=str))

    def _restore(self, now, key) -> bool:
        if not super()._restore(now, key):
            return False
        try:
            d = json.loads(self._state_file().read_text(encoding="utf-8"))
        except Exception:
            d = {}
        de = lambda l: dict(l, split_candle=dt.datetime.fromisoformat(l["split_candle"])
                            if l.get("split_candle") else None)
        self.legs = {t: (de(l) if l else None) for t, l in (d.get("legs") or {}).items()} \
            or {"CE": None, "PE": None}
        lb = d.get("leg_builders") or {}
        self.leg_builders = {t: CandleBuilder.from_dict(lb[t]) for t in LEGS if t in lb} or \
            {t: CandleBuilder(self.builder.first, self.minutes) for t in LEGS}
        for b in self.leg_builders.values():
            b.cur = None
        self.margin_halt = bool(d.get("margin_halt"))
        self.trigger.in_pos = bool((d.get("trigger") or {}).get("in_pos"))
        saved_mode = d.get("mode", "paper")
        if saved_mode == "live" and self._open() and self.live_capable:
            # the open legs are REAL: come back live, and check them against Delta
            try:
                from live.delta_client import DeltaClient
                self._client = self._client or DeltaClient()
                self.exec = LiveExec(self._client, self.live_cfg, log=self.log_line)
                self.mode = "live"
                self._reconcile(now)
            except Exception as e:
                self.audit(now, "RECONCILE_FAILED", error=str(e))
                self.mode = "live"
        self._sync_pos()
        return True

    def _reconcile(self, now):
        pos = self._client.positions()
        for t in self._open():
            l = self.legs[t]
            short = max(0, -pos.get(int(l["product_id"] or 0), 0))
            if short < l["qty"]:
                self.audit(now, "RECONCILE", leg=t, own=l["qty"], delta_short=short,
                           action="own book trimmed to what Delta holds")
                if short == 0:
                    l["exit_pending"] = "reconcile: Delta shows flat"
                else:
                    l["qty"] = short
            elif short > l["qty"]:
                self.audit(now, "RECONCILE", leg=t, own=l["qty"], delta_short=short,
                           action="extra short on Delta is NOT ours — left alone")

    # ── dashboard ────────────────────────────────────────────────────────
    def _chart_payload(self, now) -> dict:
        d = super()._chart_payload(now)
        ser = lambda c: {"t": c["start"].strftime("%Y-%m-%d %H:%M"), "o": c["open"],
                         "h": c["high"], "l": c["low"], "c": c["close"],
                         "v": c.get("volume"), "vwap": c.get("vwap")}
        d["leg_candles"] = {t: {"candles": [ser(c) for c in b.closed],
                                "forming": ({"t": b.cur["start"].strftime("%Y-%m-%d %H:%M"),
                                             "o": b.cur["open"], "h": b.cur["high"],
                                             "l": b.cur["low"], "c": b.cur["close"]}
                                            if b.cur else None)}
                            for t, b in (self.leg_builders or {}).items()}
        d["split"] = True
        return d

    def snapshot(self, now) -> dict:
        d = super().snapshot(now)
        d.update({"mode": self.mode, "legs": self.legs, "split": True,
                  "live_capable": self.live_capable, "control": self._read_control(),
                  "vwap_cap": self.vwap_cap or None,
                  "price_mtm": self.price_mtm(), "net_mtm": round(self.realized() + self.unrealized(), 4),
                  "charges": self.charges(), "gst_rate": self.gst_rate,
                  "margin_halt": self.margin_halt, "stop_basis": self.stop_basis,
                  "leg_vwap": {t: (b.live_vwap() if b else None)
                               for t, b in (self.leg_builders or {}).items()}})
        return d
