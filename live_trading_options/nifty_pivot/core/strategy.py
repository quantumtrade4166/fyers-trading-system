"""
core/strategy.py — Nifty Directional Pivot: the decision logic, and nothing else.
================================================================================

No feed, no broker, no files. Two consumers drive the same classes:

    engine.py   live: Kite ticks -> 1-min closes -> 5-min bars; paper fills at LTP
    replay.py   history: minute data -> the same calls; must reproduce the backtest
                trade-for-trade, which is the proof that live == tested

Everything that decides a trade is a line-for-line port of
backtesting/options_pivot_intraday/strategy.py::run_day. Bars, Supertrend and the
daily OHLC come from the backtest's OWN functions (imported, not re-implemented),
so the signal side cannot drift from what was tested:

    minute closes -> build_5min_bars -> add_supertrend(14, 2.5)
    previous session H/L/C (of minute closes) -> PP, R1, S1

Rules (unchanged from the backtest):
    Bullish: 5-min close > Supertrend AND close > R1  -> SELL naked ATM PUT
    Bearish: 5-min close < Supertrend AND close < S1  -> SELL naked ATM CALL
    Exit (first of): option trades at 2x entry | 5-min close crosses Supertrend
                     against the position | 15:15 square-off
    Max 2 entries/day, one position at a time, last entry on the 15:10 bar.
"""

import sys
import math
import datetime as dt
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# the backtest's own functions -- imported so the live signal is the tested signal
from backtesting.options_pivot_intraday.signals import build_5min_bars, daily_ohlc_from_spot  # noqa: E402
from backtesting.indicators import add_supertrend                                             # noqa: E402
from backtesting.options_pivot_intraday.costs import statutory_costs                          # noqa: E402

PUT, CALL = "PUT", "CALL"


def _hhmm(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


# ════════════════════════════════════════════════════════════════════════════
# signals
# ════════════════════════════════════════════════════════════════════════════
class SignalBuilder:
    """Holds the minute-close series and derives bars, Supertrend and pivots from it
    with the backtest's functions.

    The backtest's spot was one value per minute (validated against the Breeze
    index 1-min CLOSE: median error 0.00 pts). So live, a minute's value is the
    last tick received in that minute, and a 5-min bar's high/low are the extremes
    of those minute values -- NOT of every tick. Using raw tick extremes would widen
    the ATR and quietly move the Supertrend away from the tested one.
    """

    def __init__(self, period: int, multiplier: float):
        self.period, self.mult = period, multiplier
        self.minutes: dict[pd.Timestamp, float] = {}

    # ── feeding ──────────────────────────────────────────────────────────
    def load_series(self, s: pd.Series):
        for ts, v in s.items():
            self.minutes[pd.Timestamp(ts).floor("min")] = float(v)

    def set_minute(self, ts, close: float):
        self.minutes[pd.Timestamp(ts).floor("min")] = float(close)

    def series(self) -> pd.Series:
        s = pd.Series(self.minutes, dtype="float64").sort_index()
        # session minutes only (09:15..15:30), exactly the backtest's universe
        t = s.index.time
        return s[(t >= dt.time(9, 15)) & (t <= dt.time(15, 30))]

    def trim(self, keep_sessions: int):
        s = self.series()
        days = sorted(s.index.normalize().unique())
        if len(days) > keep_sessions:
            cut = days[-keep_sessions]
            self.minutes = {k: v for k, v in self.minutes.items() if k >= cut}

    def sessions(self) -> list:
        return sorted({k.normalize() for k in self.minutes})

    # ── derived ──────────────────────────────────────────────────────────
    def bars(self) -> pd.DataFrame:
        """5-min OHLC labelled by close time + Supertrend, over the whole series."""
        s = self.series()
        if s.empty:
            return pd.DataFrame()
        b = build_5min_bars(s)
        b = add_supertrend(b, period=self.period, multiplier=self.mult)
        b = b.rename(columns={f"supertrend_{self.period}_{self.mult}": "st",
                              f"supertrend_dir_{self.period}_{self.mult}": "dir"})
        return b

    def pivots_for(self, day: dt.date):
        """PP/R1/S1 for `day` from the most recent session strictly before it.
        Returns (levels_dict, prev_session_date) or (None, None)."""
        s = self.series()
        s = s[s.index.normalize() < pd.Timestamp(day)]
        if s.empty:
            return None, None
        daily = daily_ohlc_from_spot(s)
        prev = daily.iloc[-1]
        pp = (prev["high"] + prev["low"] + prev["close"]) / 3.0
        levels = {
            "pp": float(pp),
            "r1": float(2 * pp - prev["low"]),
            "s1": float(2 * pp - prev["high"]),
            "prev_high": float(prev["high"]), "prev_low": float(prev["low"]),
            "prev_close": float(prev["close"]),
        }
        return levels, daily.index[-1].date()


def previous_weekday(day: dt.date) -> dt.date:
    d = day - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


# ════════════════════════════════════════════════════════════════════════════
# costs — the SAME realistic model the backtest reported (run_costs.py)
# ════════════════════════════════════════════════════════════════════════════
def trade_costs(entry: float, exit_: float, qty: int, lots: int, date_str: str,
                reason: str, cfg: dict) -> dict:
    s_in = cfg["slippage_base_pts"] + cfg["slippage_per_lot_pts"] * lots
    s_out = s_in * (cfg["stop_slippage_mult"] if reason == "premium_stop" else 1.0)
    sell_fill = max(0.05, entry - s_in)
    buy_fill = exit_ + s_out
    stat = statutory_costs(sell_fill, buy_fill, qty, date_str)
    gross_raw = (entry - exit_) * qty
    after_slip = (sell_fill - buy_fill) * qty
    return {
        "gross": round(gross_raw, 2),
        "slippage": round(gross_raw - after_slip, 2),
        "charges": round(stat["total"], 2),
        "net": round(after_slip - stat["total"], 2),
        "stt": round(stat["stt"], 2), "exch": round(stat["exch"], 2),
        "gst": round(stat["gst"], 2), "brokerage": round(stat["brokerage"], 2),
        "stamp": round(stat["stamp"], 2), "sebi": round(stat["sebi"], 4),
    }


# ════════════════════════════════════════════════════════════════════════════
# the book
# ════════════════════════════════════════════════════════════════════════════
class PivotCore:
    """One trading day's state machine. Port of the backtest's run_day loop."""

    def __init__(self, params: dict):
        self.p = params
        self.max_entries = int(params["max_entries_per_day"])
        self.last_entry = _hhmm(params["last_entry_time"])
        self.squareoff = _hhmm(params["squareoff_time"])
        self.stop_mult = float(params["premium_stop_mult"])
        self.step = int(params["strike_step"])
        self.lots = int(params["lots"])
        self.cost_cfg = params["costs"]

        self.date: str | None = None
        self.levels: dict | None = None
        self.lot_size: int | None = None
        self.qty: int | None = None
        self.entries_used = 0
        self.pos: dict | None = None
        self.trades: list[dict] = []
        self.prev_ts: pd.Timestamp | None = None
        self.last_bar: str | None = None       # label of the last bar processed

    # ── day setup ────────────────────────────────────────────────────────
    def start_day(self, date_str: str, levels: dict, lot_size: int):
        self.date, self.levels = date_str, levels
        self.lot_size = int(lot_size)
        self.qty = self.lots * self.lot_size
        self.entries_used, self.pos, self.trades = 0, None, []
        self.prev_ts, self.last_bar = None, None

    def round_to_atm(self, spot: float) -> int:
        return int(round(spot / self.step) * self.step)

    @staticmethod
    def _intrinsic(strike, spot, opt):
        return max(0.0, strike - spot) if opt == PUT else max(0.0, spot - strike)

    # ── closing a position ───────────────────────────────────────────────
    def _close(self, ts, exit_price, reason, spot, approx=False) -> dict:
        pos = self.pos
        c = trade_costs(pos["entry_price"], exit_price, self.qty, self.lots,
                        self.date, reason, self.cost_cfg)
        t = {**pos, "exit_time": pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
             "exit_price": float(exit_price), "exit_reason": reason,
             "spot_at_exit": round(float(spot), 2), "exit_approx": bool(approx),
             **{f"pnl_{k}": v for k, v in c.items()}}
        self.trades.append(t)
        self.pos = None
        return t

    # ── the bar step (exact port of the backtest loop body) ──────────────
    def on_bar(self, ts, close: float, direction: int, st_line: float,
               price_fn, high_fn=None) -> list[dict]:
        """Process one completed 5-min bar labelled `ts` (its close time).

        price_fn(strike, opt) -> fill price at `ts`, or None if not quoted.
        high_fn(strike, opt, prev_ts, ts) -> max premium in (prev_ts, ts] or None.
            Replay passes it to reproduce the backtest's bar-window stop check.
            Live leaves it None: the stop is enforced on every tick in on_option_tick.
        Returns the events this bar produced (entries/exits).
        """
        ts = pd.Timestamp(ts)
        events = []
        bar_time = ts.time()
        direction = int(direction)
        close = float(close)
        st_line = float(st_line)
        r1, s1 = self.levels["r1"], self.levels["s1"]

        bull = (direction == 1) and (close > r1) and (close > st_line)
        bear = (direction == -1) and (close < s1) and (close < st_line)

        if self.pos is not None:
            opt = self.pos["option_type"]
            adverse = (opt == PUT and direction == -1) or (opt == CALL and direction == 1)
            forced = bar_time >= self.squareoff

            stop_hit = False
            if high_fn is not None and self.prev_ts is not None:
                hi = high_fn(self.pos["strike"], opt, self.prev_ts, ts)
                if hi is not None and hi >= self.pos["stop_level"]:
                    stop_hit = True

            if stop_hit or adverse or forced:
                if stop_hit:
                    ev = self._close(ts, self.pos["stop_level"], "premium_stop", close)
                else:
                    px = price_fn(self.pos["strike"], opt)
                    approx = px is None
                    if approx:
                        px = self._intrinsic(self.pos["strike"], close, opt)
                    ev = self._close(ts, px, "squareoff_1515" if forced else "supertrend_flip",
                                     close, approx)
                events.append({"type": "exit", **ev})
            else:
                self.prev_ts = ts
                self.last_bar = ts.strftime("%H:%M")
                return events

        if (self.pos is None and self.entries_used < self.max_entries
                and bar_time <= self.last_entry):
            opt = PUT if bull else (CALL if bear else None)
            if opt is not None:
                strike = self.round_to_atm(close)
                px = price_fn(strike, opt)
                if px is not None and px > 0:
                    self.entries_used += 1
                    self.pos = {
                        "date": self.date,
                        "entry_time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "option_type": opt,
                        "side": "short_put" if opt == PUT else "short_call",
                        "strike": strike, "entry_price": float(px),
                        "spot_at_entry": round(close, 2),
                        "r1": round(r1, 2), "s1": round(s1, 2),
                        "st_at_entry": round(st_line, 2),
                        "entry_no": self.entries_used,
                        "stop_level": float(px) * self.stop_mult,
                        "qty": self.qty, "lots": self.lots, "lot_size": self.lot_size,
                    }
                    events.append({"type": "entry", **self.pos})

        self.prev_ts = ts
        self.last_bar = ts.strftime("%H:%M")
        return events

    # ── live-only: tick-level premium stop ───────────────────────────────
    def on_option_tick(self, ts, ltp: float, spot: float) -> dict | None:
        if self.pos is None or ltp is None:
            return None
        if float(ltp) >= self.pos["stop_level"]:
            ev = self._close(ts, float(ltp), "premium_stop", spot)
            return {"type": "exit", **ev}
        return None

    def force_close(self, ts, ltp, spot, reason: str) -> dict | None:
        """Used only by the engine's safety net (e.g. past square-off with a feed gap)."""
        if self.pos is None:
            return None
        px = ltp if ltp is not None else self._intrinsic(self.pos["strike"], spot, self.pos["option_type"])
        return {"type": "exit", **self._close(ts, px, reason, spot, approx=ltp is None)}

    # ── marking ──────────────────────────────────────────────────────────
    def mtm(self, ltp: float | None, ts=None) -> dict:
        realized_gross = sum(t["pnl_gross"] for t in self.trades)
        realized_net = sum(t["pnl_net"] for t in self.trades)
        open_gross = open_net = 0.0
        if self.pos is not None and ltp is not None:
            c = trade_costs(self.pos["entry_price"], float(ltp), self.qty, self.lots,
                            self.date, "mark", self.cost_cfg)
            open_gross, open_net = c["gross"], c["net"]
        return {
            "realized_gross": round(realized_gross, 2), "realized_net": round(realized_net, 2),
            "open_gross": round(open_gross, 2), "open_net": round(open_net, 2),
            "day_gross": round(realized_gross + open_gross, 2),
            "day_net": round(realized_net + open_net, 2),
        }

    # ── persistence (restart recovery) ───────────────────────────────────
    def to_dict(self) -> dict:
        return {"date": self.date, "levels": self.levels, "lot_size": self.lot_size,
                "qty": self.qty, "entries_used": self.entries_used, "pos": self.pos,
                "trades": self.trades, "last_bar": self.last_bar,
                "prev_ts": self.prev_ts.strftime("%Y-%m-%d %H:%M:%S") if self.prev_ts is not None else None}

    def restore(self, d: dict):
        self.date, self.levels = d["date"], d["levels"]
        self.lot_size, self.qty = d["lot_size"], d["qty"]
        self.entries_used, self.pos = d["entries_used"], d["pos"]
        self.trades, self.last_bar = d["trades"], d.get("last_bar")
        self.prev_ts = pd.Timestamp(d["prev_ts"]) if d.get("prev_ts") else None
