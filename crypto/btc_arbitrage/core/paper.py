"""
core/paper.py — zero-fee PAPER trading of the Delta vs Binance spread, 1 BTC a side.
===================================================================================

PAPER ONLY. Nothing here talks to an exchange; fills are priced by walking the
order books the monitor already holds.

Prices for SIZE (default 1 BTC), from depth on both exchanges (core/depth.walk):

    s1 = sell-1-BTC on Binance (walk bids) − buy-1-BTC on Delta (walk asks)
    s2 = sell-1-BTC on Delta   (walk bids) − buy-1-BTC on Binance (walk asks)

Rules (all editable from the dashboard toggle panel, data/state/paper_control.json):

    ENTRY    flat, data clean, and s1 or s2 >= entry_usd ("major" arbitrage, per BTC)
             -> BUY the cheap exchange / SELL the rich one, SIZE BTC each side
    LATENCY  the fill is priced on the books latency_ms AFTER the signal — the
             time a real pair of market orders would take — not at the signal.
             If the gap vanished in that time, the paper trade eats it, as a real
             one would.
    EXIT     the unwind spread (s2 for a buy-Delta position, s1 for buy-Binance)
             >= −exit_band_usd, i.e. the books have converged and the position can
             be closed losing at most exit_band_usd of the entry gap. Also priced
             latency_ms after the signal, from depth.
    TOGGLE   OFF stops new entries AND closes an open position at current depth.

P&L per trade = (entry spread + exit spread) x SIZE — fees ZERO by design.
`pnl_with_fees` alongside shows what the configured taker fees would have taken
(4 fills), so the zero-fee result is never mistaken for a tradeable one.

Economics (buy Delta / sell Binance):
    open   buy Delta @ D_ask1, sell Binance @ B_bid1
    close  sell Delta @ D_bid2, buy Binance @ B_ask2
    P&L = (D_bid2 − D_ask1) + (B_bid1 − B_ask2) = s1_entry + s2_exit   (tests prove it)
"""

import json
import os
import time
import datetime as dt
from pathlib import Path

from core.depth import walk
from core.spread_engine import BUY_DELTA, BUY_BINANCE, LABEL

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

DEFAULTS = {"enabled": False, "entry_usd": 20.0, "exit_band_usd": 1.0, "latency_ms": 300}
LIMITS = {"entry_usd": (0.5, 10_000.0), "exit_band_usd": (0.0, 1_000.0), "latency_ms": (0, 5_000)}


def clean_control(d: dict) -> dict:
    """Validate a control dict (from the file or the dashboard). Bad values -> defaults."""
    out = dict(DEFAULTS)
    if not isinstance(d, dict):
        return out
    out["enabled"] = bool(d.get("enabled", False))
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(d.get(k, DEFAULTS[k]))
            if lo <= v <= hi:
                out[k] = v if k != "latency_ms" else int(v)
        except (TypeError, ValueError):
            pass
    return out


def depth_spreads(delta_depth, binance_depth, size_btc: float):
    """1-BTC executable prices + both spreads from the two books, or None if
    either book is too thin for the size."""
    if delta_depth is None or binance_depth is None:
        return None
    d_ask, d_bid = walk(delta_depth.asks, size_btc), walk(delta_depth.bids, size_btc)
    b_ask, b_bid = walk(binance_depth.asks, size_btc), walk(binance_depth.bids, size_btc)
    if None in (d_ask, d_bid, b_ask, b_bid):
        return None
    return {"d_bid": d_bid, "d_ask": d_ask, "b_bid": b_bid, "b_ask": b_ask,
            "s1": b_bid - d_ask, "s2": d_bid - b_ask}


def _now_ms():
    return time.time() * 1000


def _ist(ms):
    return dt.datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class PaperTrader:
    """State machine: FLAT -> ENTERING -> OPEN -> EXITING -> FLAT.

    `step()` is called on every evaluation with the current depth spreads (or
    None when depth is missing/stale). It is pure bookkeeping — no I/O except
    the state / trades files and the event callback."""

    def __init__(self, size_btc: float, fee_model, control_file: Path, state_file: Path,
                 trades_file: Path, on_event=None, log=None):
        self.size = size_btc
        self.fees = fee_model
        self.control_file, self.state_file, self.trades_file = control_file, state_file, trades_file
        self.on_event = on_event or (lambda e: None)
        self.log = log
        self.control = dict(DEFAULTS)
        self._control_mtime = None
        self.state = "FLAT"
        self.pending = None          # {"kind","direction","signal_ms","due_mono","signal_spread",...}
        self.position = None         # open position dict
        self.last_spreads = None
        self.trades_count = 0
        self.realized = 0.0
        self.realized_fees = 0.0
        self._load_state()

    # ── control (written by the dashboard) ───────────────────────────────
    def refresh_control(self):
        try:
            m = os.path.getmtime(self.control_file)
        except OSError:
            return
        if m == self._control_mtime:
            return
        self._control_mtime = m
        try:
            new = clean_control(json.loads(Path(self.control_file).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return
        if new != self.control:
            old = self.control
            self.control = new
            self._emit({"event": "PAPER_CONTROL", "enabled": new["enabled"],
                        "entry_usd": new["entry_usd"], "exit_band_usd": new["exit_band_usd"],
                        "latency_ms": new["latency_ms"],
                        "reason": "switched ON" if new["enabled"] and not old["enabled"] else
                                  ("switched OFF" if old["enabled"] and not new["enabled"] else "parameters changed")})
            self._save_state()

    # ── the step ─────────────────────────────────────────────────────────
    def step(self, sp, data_ok: bool, now_mono: float = None):
        """sp = depth_spreads(...) or None. data_ok = top-of-book quality is OK and
        both depth books are fresh."""
        now_mono = time.monotonic() if now_mono is None else now_mono
        self.last_spreads = sp if data_ok else None
        c = self.control

        # due fills first — a pending order fills on whatever the books are NOW
        if self.pending and now_mono >= self.pending["due_mono"]:
            self._fill(sp if data_ok else None, now_mono)

        if self.state == "FLAT" and c["enabled"] and data_ok and sp:
            if sp["s1"] >= c["entry_usd"]:
                self._signal("ENTRY", BUY_DELTA, sp["s1"], now_mono)
            elif sp["s2"] >= c["entry_usd"]:
                self._signal("ENTRY", BUY_BINANCE, sp["s2"], now_mono)
        elif self.state == "OPEN":
            if not c["enabled"]:
                self._signal("EXIT", self.position["direction"], self._unwind(sp), now_mono,
                             reason="toggle OFF")
            elif data_ok and sp:
                u = self._unwind(sp)
                if u >= -c["exit_band_usd"]:
                    self._signal("EXIT", self.position["direction"], u, now_mono,
                                 reason="converged")

    def _unwind(self, sp):
        if not sp or not self.position:
            return None
        return sp["s2"] if self.position["direction"] == BUY_DELTA else sp["s1"]

    def _signal(self, kind, direction, spread, now_mono, reason=""):
        self.state = "ENTERING" if kind == "ENTRY" else "EXITING"
        self.pending = {"kind": kind, "direction": direction, "signal_ms": _now_ms(),
                        "signal_spread": spread, "reason": reason,
                        "due_mono": now_mono + self.control["latency_ms"] / 1000}
        if self.control["latency_ms"] == 0:
            self.pending["due_mono"] = now_mono      # fills on this same step
            self._fill(self.last_spreads, now_mono)
        else:
            self._save_state()

    def _fill(self, sp, now_mono):
        p = self.pending
        if sp is None:
            if p["kind"] == "ENTRY":
                # no book at fill time -> a real order would not have been sent; drop it
                self._emit({"event": "PAPER_SKIP", "direction": p["direction"],
                            "reason": "depth/data not clean at fill time"})
                self.pending, self.state = None, "FLAT"
                self._save_state()
            else:
                p["due_mono"] = now_mono + 0.25     # an exit must happen: retry shortly
            return
        d = p["direction"]
        if p["kind"] == "ENTRY":
            if d == BUY_DELTA:
                d_px, b_px, edge = sp["d_ask"], sp["b_bid"], sp["s1"]
            else:
                d_px, b_px, edge = sp["d_bid"], sp["b_ask"], sp["s2"]
            self.position = {"direction": d, "size_btc": self.size,
                             "signal_ms": p["signal_ms"], "signal_spread": round(p["signal_spread"], 2),
                             "entry_ms": _now_ms(), "entry_spread": round(edge, 4),
                             "entry_delta_px": round(d_px, 2), "entry_binance_px": round(b_px, 2),
                             "slippage_vs_signal": round(edge - p["signal_spread"], 4)}
            self.state, self.pending = "OPEN", None
            self._emit({"event": "PAPER_ENTRY", "direction": d, "label": LABEL[d],
                        "signal_spread": round(p["signal_spread"], 2), "fill_spread": round(edge, 2),
                        "delta_px": round(d_px, 2), "binance_px": round(b_px, 2),
                        "size_btc": self.size})
        else:
            pos = self.position
            if d == BUY_DELTA:                      # close: sell Delta @bid, buy Binance @ask
                d_px, b_px, u = sp["d_bid"], sp["b_ask"], sp["s2"]
            else:                                   # close: sell Binance @bid, buy Delta @ask
                d_px, b_px, u = sp["d_ask"], sp["b_bid"], sp["s1"]
            pnl = (pos["entry_spread"] + u) * self.size
            fee = (self.fees.cost(pos["entry_delta_px"], pos["entry_binance_px"]) / self.fees.legs
                   + self.fees.cost(d_px, b_px) / self.fees.legs) * self.size
            exit_ms = _now_ms()
            trade = {**pos, "exit_ms": exit_ms, "exit_spread": round(u, 4),
                     "exit_delta_px": round(d_px, 2), "exit_binance_px": round(b_px, 2),
                     "exit_reason": p["reason"], "exit_signal_spread": round(p["signal_spread"], 2)
                     if p["signal_spread"] is not None else None,
                     "hold_s": round((exit_ms - pos["entry_ms"]) / 1000, 1),
                     "pnl": round(pnl, 2), "fees_if_charged": round(fee, 2),
                     "pnl_with_fees": round(pnl - fee, 2),
                     "entry_ts": _ist(pos["entry_ms"]), "exit_ts": _ist(exit_ms)}
            self.trades_count += 1
            self.realized += trade["pnl"]
            self.realized_fees += trade["fees_if_charged"]
            try:
                with open(self.trades_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(trade) + "\n")
            except OSError as e:
                if self.log:
                    self.log.error(f"paper trades write failed: {e}")
            self.position, self.pending, self.state = None, None, "FLAT"
            self._emit({"event": "PAPER_EXIT", "direction": d, "label": LABEL[d],
                        "fill_spread": round(u, 2), "pnl": trade["pnl"],
                        "pnl_with_fees": trade["pnl_with_fees"], "hold_s": trade["hold_s"],
                        "reason": p["reason"]})
        self._save_state()

    # ── views ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        pos = None
        if self.position:
            u = self._unwind(self.last_spreads)
            pos = {**self.position, "label": LABEL[self.position["direction"]],
                   "entry_ts": _ist(self.position["entry_ms"]),
                   "unwind_spread": round(u, 2) if u is not None else None,
                   "mtm": round((self.position["entry_spread"] + u) * self.size, 2) if u is not None else None,
                   "held_s": round((_now_ms() - self.position["entry_ms"]) / 1000)}
        sp = self.last_spreads
        return {"control": self.control, "state": self.state, "size_btc": self.size,
                "position": pos,
                "pending": {k: v for k, v in self.pending.items() if k != "due_mono"} if self.pending else None,
                "spreads_1btc": {k: round(v, 2) for k, v in sp.items()} if sp else None,
                "trades": self.trades_count, "realized": round(self.realized, 2),
                "realized_fees_if_charged": round(self.realized_fees, 2)}

    # ── persistence: an open paper position survives a restart ──────────
    def _save_state(self):
        st = {"control": self.control, "state": self.state if self.state in ("FLAT", "OPEN") else
              ("OPEN" if self.position else "FLAT"), "position": self.position,
              "trades": self.trades_count, "realized": self.realized,
              "realized_fees": self.realized_fees}
        try:
            tmp = Path(str(self.state_file) + ".tmp")
            tmp.write_text(json.dumps(st), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            try:
                Path(self.state_file).write_text(json.dumps(st), encoding="utf-8")
            except OSError:
                pass

    def _load_state(self):
        try:
            c = json.loads(Path(self.control_file).read_text(encoding="utf-8"))
            self.control = clean_control(c)
            self._control_mtime = os.path.getmtime(self.control_file)
        except (OSError, ValueError):
            pass
        try:
            st = json.loads(Path(self.state_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.position = st.get("position")
        self.state = "OPEN" if self.position else "FLAT"
        self.trades_count = int(st.get("trades") or 0)
        self.realized = float(st.get("realized") or 0)
        self.realized_fees = float(st.get("realized_fees") or 0)

    def _emit(self, e):
        try:
            self.on_event(e)
        except Exception:
            pass
