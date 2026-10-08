import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Generic Signal → Backtest Harness (Kaufman catalog)
======================================================
Every strategy in the catalog is expressed as a *signal function*:

    def my_strategy(df: pd.DataFrame, cfg: dict) -> pd.DataFrame

returning a frame (same index as df) with these columns:

    entry        +1 = go long, -1 = go short, 0 = nothing
    exit_long    bool — close an open long this bar
    exit_short   bool — close an open short this bar
    stop_long    float — stop price to use when opening a long  (NaN = auto)
    stop_short   float — stop price to use when opening a short (NaN = auto)

The harness then handles — identically for every strategy, so results are
directly comparable:
  • position state, entry on close or next open
  • intrabar stop-loss checks (High/Low, checked BEFORE signal exits)
  • reversal ("always in market") vs flat-between-trades modes
  • time exits
  • risk-based position sizing (1% of capital / stop distance)
  • trade records + the standard metric set

Config keys (all optional, sane defaults):
    mode                 "flat" | "reversal"        default "flat"
    entry_on             "close" | "next_open"      default "close"
    max_bars             int, time exit             default None
    capital              float                      default 20_000
    risk_pct             float                      default 0.01
    max_position_pct     float                      default 0.50
    default_stop_atr     float, ATR mult fallback   default 2.0
    atr_col              str                        default "atr_14"
    trailing             bool — ratchet the stop    default False
                         each bar toward the strategy's current
                         stop_long/stop_short level (never against
                         the position). Needed by every trailing-stop
                         system in the catalog (S007, S016/17, S118,
                         S128-S130).
"""

from dataclasses import dataclass, asdict
from typing import Optional, Callable

import numpy as np
import pandas as pd

HARNESS_DEFAULTS = {
    "mode":             "flat",
    "entry_on":         "close",
    "max_bars":         None,
    "capital":          20_000.0,
    "risk_pct":         0.01,
    "max_position_pct": 0.50,
    "default_stop_atr": 2.0,
    "atr_col":          "atr_14",
    "trailing":         False,
    # Round-trip trading cost in PRICE UNITS per ounce (USD/oz), charged
    # once per completed trade. Covers spread + slippage + commission.
    # XAUUSD reference points:
    #   0.00  frictionless (backtest fantasy — baseline only)
    #   0.20  tight ECN: ~15c spread + commission, minimal slippage
    #   0.35  typical retail: ~30c spread + a little slippage
    #   0.50  retail with real slippage on market orders
    #   0.70  wide spread / fast market conditions
    "cost_per_unit":    0.0,
    # Round-trip cost as a FRACTION OF NOTIONAL (entry + exit value), for
    # venues that charge a percentage fee. Needed for BTC, where price went
    # $3k -> $120k and a fixed per-unit cost would be wrong at one end or the
    # other. Delta Exchange taker 0.05%/side + slippage ≈ 0.0006 per side.
    "cost_pct":         0.0,
    # Perpetual-futures funding, annualised fraction of notional paid by
    # longs (and received by shorts) while a position is held. 0 = spot.
    "funding_apr":      0.0,
    # Sharpe/drawdown calendar: "business" (weekday markets) or "calendar"
    # (24/7 markets — crypto; annualised with sqrt(365)).
    "calendar":         "business",
}


@dataclass
class Trade:
    direction:   str
    entry_time:  pd.Timestamp
    entry_price: float
    stop_loss:   float
    units:       float
    exit_time:   pd.Timestamp
    exit_price:  float
    exit_reason: str          # stop | signal | reverse | time | eod
    pnl:         float        # net of trading cost
    gross_pnl:   float        # before cost
    cost:        float        # round-trip cost charged on this trade
    bars_held:   int

    def to_dict(self) -> dict:
        return asdict(self)


def _size(risk_amount: float, sl_distance: float, entry_price: float,
          max_capital: float) -> float:
    if sl_distance <= 0 or entry_price <= 0:
        return 0.0
    return min(risk_amount / sl_distance, max_capital / entry_price)


def run(df: pd.DataFrame, signals: pd.DataFrame, cfg: dict = None) -> list[Trade]:
    """
    Walk the bars and turn signals into trades.

    df      : OHLC frame (datetime index), must include the ATR column if
              any strategy relies on the automatic stop fallback
    signals : frame from a strategy function (see module docstring)
    """
    c = {**HARNESS_DEFAULTS, **(cfg or {})}

    risk_amount = c["capital"] * c["risk_pct"]
    max_cap     = c["capital"] * c["max_position_pct"]
    reversal    = c["mode"] == "reversal"
    next_open   = c["entry_on"] == "next_open"
    max_bars    = c["max_bars"]

    idx    = df.index
    open_  = df["open"].to_numpy(dtype=float)
    high   = df["high"].to_numpy(dtype=float)
    low    = df["low"].to_numpy(dtype=float)
    close  = df["close"].to_numpy(dtype=float)

    atr = (df[c["atr_col"]].to_numpy(dtype=float)
           if c["atr_col"] in df.columns else np.full(len(df), np.nan))

    entry_sig  = signals["entry"].fillna(0).to_numpy(dtype=int)
    exit_long  = signals["exit_long"].fillna(False).to_numpy(dtype=bool)
    exit_short = signals["exit_short"].fillna(False).to_numpy(dtype=bool)
    stop_long  = (signals["stop_long"].to_numpy(dtype=float)
                  if "stop_long" in signals else np.full(len(df), np.nan))
    stop_short = (signals["stop_short"].to_numpy(dtype=float)
                  if "stop_short" in signals else np.full(len(df), np.nan))

    n = len(df)
    trades: list[Trade] = []

    pos_dir   = 0        # 0 flat, +1 long, -1 short
    pos_entry = 0.0
    pos_stop  = 0.0
    pos_units = 0.0
    pos_bar   = -1
    pos_time  = None

    pending = 0          # for entry_on="next_open": direction queued last bar
    pend_sl_long = np.nan
    pend_sl_short = np.nan

    def _open(direction: int, i: int, price: float,
              sl_long: float, sl_short: float) -> None:
        nonlocal pos_dir, pos_entry, pos_stop, pos_units, pos_bar, pos_time
        raw_stop = sl_long if direction > 0 else sl_short
        if np.isnan(raw_stop):
            a = atr[i]
            if np.isnan(a) or a <= 0:
                return
            raw_stop = (price - c["default_stop_atr"] * a if direction > 0
                        else price + c["default_stop_atr"] * a)
        dist = abs(price - raw_stop)
        units = _size(risk_amount, dist, price, max_cap)
        if units <= 0:
            return
        pos_dir, pos_entry, pos_stop = direction, price, raw_stop
        pos_units, pos_bar, pos_time = units, i, idx[i]

    cost_per_unit = c["cost_per_unit"]
    cost_pct      = c["cost_pct"]
    funding_apr   = c["funding_apr"]

    def _close(i: int, price: float, reason: str) -> None:
        nonlocal pos_dir
        gross = ((price - pos_entry) if pos_dir > 0 else (pos_entry - price)) * pos_units
        cost = cost_per_unit * pos_units + cost_pct * (pos_entry + price) * pos_units
        if funding_apr:
            years_held = (idx[i] - pos_time).total_seconds() / (365.25 * 86400)
            cost += pos_dir * funding_apr * years_held * pos_entry * pos_units
        pnl = gross - cost
        trades.append(Trade(
            direction   = "long" if pos_dir > 0 else "short",
            entry_time  = pos_time,
            entry_price = round(pos_entry, 4),
            stop_loss   = round(pos_stop, 4),
            units       = round(pos_units, 4),
            exit_time   = idx[i],
            exit_price  = round(price, 4),
            exit_reason = reason,
            pnl         = round(pnl, 2),
            gross_pnl   = round(gross, 2),
            cost        = round(cost, 2),
            bars_held   = i - pos_bar,
        ))
        pos_dir = 0

    for i in range(n):
        # ── Fill a queued next-open entry ────────────────────────────────
        if pending != 0 and pos_dir == 0:
            _open(pending, i, open_[i], pend_sl_long, pend_sl_short)
        pending = 0

        # ── Manage an open position ──────────────────────────────────────
        if pos_dir != 0:
            # 0. Trailing stop — ratchet toward the strategy's level, never
            #    loosening it. Uses the level known at the PREVIOUS close:
            #    using stop_long[i] here would let the current bar's High set
            #    the stop and then test the same bar's Low against it, which
            #    books exits at prices that may never have been reachable in
            #    sequence (a lookahead that manufactures a fake edge).
            if c["trailing"] and i > 0:
                if pos_dir > 0 and not np.isnan(stop_long[i - 1]):
                    pos_stop = max(pos_stop, stop_long[i - 1])
                elif pos_dir < 0 and not np.isnan(stop_short[i - 1]):
                    pos_stop = min(pos_stop, stop_short[i - 1])

            # 1. Stop loss first — intrabar, worst case assumed.
            #    A bar that opens beyond the stop gapped through it, so the
            #    realistic fill is the open, not the stop level.
            if pos_dir > 0 and low[i] <= pos_stop:
                _close(i, min(pos_stop, open_[i]), "stop")
            elif pos_dir < 0 and high[i] >= pos_stop:
                _close(i, max(pos_stop, open_[i]), "stop")

            # 2. Time exit
            if pos_dir != 0 and max_bars is not None and (i - pos_bar) >= max_bars:
                _close(i, close[i], "time")

            # 3. Strategy exit signal
            if pos_dir > 0 and exit_long[i]:
                _close(i, close[i], "signal")
            elif pos_dir < 0 and exit_short[i]:
                _close(i, close[i], "signal")

            # 4. Reversal: opposite entry closes and flips
            if pos_dir != 0 and reversal and entry_sig[i] != 0 and entry_sig[i] != pos_dir:
                new_dir = entry_sig[i]
                _close(i, close[i], "reverse")
                if next_open:
                    pending = new_dir
                    pend_sl_long, pend_sl_short = stop_long[i], stop_short[i]
                else:
                    _open(new_dir, i, close[i], stop_long[i], stop_short[i])
                continue

        # ── Flat: look for a new entry ───────────────────────────────────
        if pos_dir == 0 and entry_sig[i] != 0:
            if next_open:
                pending = entry_sig[i]
                pend_sl_long, pend_sl_short = stop_long[i], stop_short[i]
            else:
                _open(entry_sig[i], i, close[i], stop_long[i], stop_short[i])

    if pos_dir != 0:
        _close(n - 1, close[-1], "eod")

    return trades


# ── Metrics ──────────────────────────────────────────────────────────────

def metrics(trades: list[Trade], capital: float,
            bars_per_year: Optional[float] = None,
            calendar: str = "business") -> dict:
    """Standard metric set — identical for every strategy so results compare."""
    if not trades:
        return {"total_trades": 0, "cagr_pct": 0.0, "sharpe": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0,
                "win_rate_pct": 0.0, "net_return_pct": 0.0, "avg_rr": 0.0,
                "net_pnl_usd": 0.0, "final_equity_usd": capital,
                "gross_pnl_usd": 0.0, "total_cost_usd": 0.0}

    df = pd.DataFrame([t.to_dict() for t in trades])
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["exit_time"]  = pd.to_datetime(df["exit_time"])

    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] < 0]

    total_pnl = df["pnl"].sum()
    gross_p   = wins["pnl"].sum()
    gross_l   = abs(losses["pnl"].sum())
    pf        = gross_p / gross_l if gross_l > 0 else float("inf")

    avg_win  = wins["pnl"].mean()   if len(wins)   else 0.0
    avg_loss = abs(losses["pnl"].mean()) if len(losses) else 0.0
    avg_rr   = avg_win / avg_loss if avg_loss > 0 else float("inf")

    d0, d1 = df["entry_time"].min().date(), df["exit_time"].max().date()
    years  = max((d1 - d0).days, 1) / 365.25
    final  = capital + total_pnl
    cagr   = ((final / capital) ** (1 / years) - 1) * 100 if final > 0 else -100.0

    df["exit_date"] = df["exit_time"].dt.date
    daily_traded = df.groupby("exit_date")["pnl"].sum()

    # Sharpe MUST be computed over the full calendar of trading days with
    # flat days filled as zero. Using only days that happened to have a trade
    # and then annualising by sqrt(252) inflates Sharpe by 1/sqrt(fraction
    # of days traded) — e.g. a strategy trading 9 times a year scored 4.4
    # instead of its true 0.75.
    # Union of business days AND actual exit dates: forex bars can be stamped
    # on the Sunday 22:00 session boundary, which is not a business day — a
    # plain bdate_range reindex silently DROPS those trades' PnL, which zeroed
    # out every weekly-timeframe Sharpe and drawdown.
    crypto = calendar == "calendar"
    span = pd.date_range(d0, d1) if crypto else pd.bdate_range(d0, d1)
    all_days = sorted(set(span.date) | set(daily_traded.index))
    daily = daily_traded.reindex(all_days, fill_value=0.0)

    sharpe = ((daily.mean() / daily.std()) * np.sqrt(365 if crypto else 252)
              if len(daily) > 1 and daily.std() > 0 else 0.0)

    cum    = daily.cumsum() + capital
    max_dd = ((cum - cum.cummax()) / cum.cummax() * 100).min()

    reasons = df["exit_reason"].value_counts().to_dict()

    return {
        "total_trades":     len(df),
        "long_trades":      int((df["direction"] == "long").sum()),
        "short_trades":     int((df["direction"] == "short").sum()),
        "win_rate_pct":     round(len(wins) / len(df) * 100, 1),
        "profit_factor":    round(pf, 3),
        "avg_rr":           round(avg_rr, 2),
        "sharpe":           round(sharpe, 3),
        "cagr_pct":         round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "net_return_pct":   round(total_pnl / capital * 100, 2),
        "net_pnl_usd":      round(total_pnl, 2),
        "final_equity_usd": round(capital + total_pnl, 2),
        "gross_pnl_usd":    round(df["gross_pnl"].sum(), 2),
        "total_cost_usd":   round(df["cost"].sum(), 2),
        "avg_bars_held":    round(df["bars_held"].mean(), 1),
        "pct_days_traded":  round(len(daily_traded) / max(len(all_days), 1) * 100, 1),
        "stops":            reasons.get("stop", 0),
        "signal_exits":     reasons.get("signal", 0),
        "reversals":        reasons.get("reverse", 0),
        "time_exits":       reasons.get("time", 0),
        "date_start":       str(d0),
        "date_end":         str(d1),
    }


def blank_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Empty signal frame a strategy can fill in."""
    return pd.DataFrame({
        "entry":      0,
        "exit_long":  False,
        "exit_short": False,
        "stop_long":  np.nan,
        "stop_short": np.nan,
    }, index=df.index)


def backtest(df: pd.DataFrame, strategy: Callable, cfg: dict = None) -> tuple:
    """Convenience: run a strategy function end-to-end. Returns (trades, metrics)."""
    c = {**HARNESS_DEFAULTS, **(cfg or {})}
    sig = strategy(df, c)
    trades = run(df, sig, c)
    return trades, metrics(trades, c["capital"], calendar=c["calendar"])
