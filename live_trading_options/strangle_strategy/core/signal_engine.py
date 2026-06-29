"""
core/signal_engine.py
=====================

Strategy entry/exit logic over combined-premium 5-min candles.

Turns the raw "candle vs VWAP" conditions into the actual sequence of trades.

Rules (from the spec):
  - Strikes fixed at 9:20; trading starts from the 9:20 candle onward.
  - ONE position open at a time. Strict alternation:
        entry 1 -> exit 1 -> entry 2 -> exit 2 -> ... up to 4 cycles.
    A new entry can only fire once the previous entry has exited.
  - ENTRY : a 5-min candle closes BELOW VWAP and is RED (close < open)
            -> sell the strangle (limit at candle_low - 1). One fill.
  - EXIT  : while in a position, a candle closes ABOVE VWAP
            -> buy back at market (candle close).
  - Max 4 filled entries per day; the count never resets.

P&L is for a SHORT strangle: pnl_points = entry_price - exit_price
(sell high, buy back low). Rupees = points * lot_size * lots.

5-min-close approximation (fills assumed at low-1). Live engine uses ticks +
real fills; this shared module is the canonical rule set.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as _dt
import pandas as pd

MAX_ENTRIES_PER_DAY = 4
ENTRY_CUTOFF = "14:30"     # no NEW entries after this (intraday)
SQUARE_OFF   = "15:15"     # force-close any open position at this time (intraday)


def _t(s: str) -> _dt.time:
    return _dt.datetime.strptime(s, "%H:%M").time()


def simulate_day(combined: pd.DataFrame, max_entries: int = MAX_ENTRIES_PER_DAY,
                 entry_cutoff: str = ENTRY_CUTOFF, square_off: str = SQUARE_OFF) -> list[dict]:
    """Ordered entry/exit events, strictly alternating (one position at a time).

    Entry is a LIMIT order, not an instant fill:
      - A red candle closing below its VWAP places a sell limit at `low - 1`.
      - The limit FILLS only if a later candle actually trades down to that price
        (candle low <= limit). It counts as an entry only when filled.
      - If not filled by the next candle's close: cancel if that candle closed
        ABOVE VWAP; or, if that candle is itself a fresh red-below-VWAP signal,
        replace the limit at the new low - 1; otherwise cancel.
      - The 9:15 candle (closes at 9:20, the strike-selection moment) is a valid
        signal bar.

    Intraday guards:
      - no entry FILL after `entry_cutoff` (2:30 PM)
      - any open position force-closed at `square_off` (3:15 PM) at that candle's open

    Each event: {time, type:'entry'|'exit', price, fill_no (entries), reason}
    """
    cutoff, sq = _t(entry_cutoff), _t(square_off)
    events: list[dict] = []
    entries = 0
    in_pos = False
    pending = None        # resting sell-limit price while flat (else None)
    sig_time = None       # signal candle of the resting limit (for the reason text)

    for _, r in combined.iterrows():
        tt = r["datetime"].time()
        hm = r["datetime"].strftime("%H:%M")
        low, close = float(r["low"]), float(r["close"])
        is_red, below, above = bool(r["is_red"]), bool(r["below_vwap"]), bool(r["above_vwap"])

        # ── manage an open position ───────────────────────────────────────────
        if in_pos:
            if tt >= sq:                              # intraday square-off at 3:15
                events.append({"time": hm, "type": "exit", "price": round(float(r["open"]), 2),
                               "reason": f"intraday square-off {square_off}"})
                in_pos = False
                break
            if above:                                # exit on close above VWAP
                events.append({"time": hm, "type": "exit", "price": round(close, 2),
                               "reason": "close above VWAP"})
                in_pos = False
            continue

        # ── flat: first see if a resting limit fills on THIS candle ───────────
        if pending is not None:
            if tt <= cutoff and low <= pending:      # price traded down to the limit -> FILL
                entries += 1
                events.append({"time": hm, "type": "entry", "price": round(pending, 2),
                               "fill_no": entries,
                               "reason": f"limit {pending:.2f} (signal {sig_time}) filled"})
                in_pos = True
                pending = sig_time = None
                continue
            # not filled — cancel or replace the resting limit
            if above:
                pending = sig_time = None                         # cancel: closed above VWAP
            elif is_red and below and entries < max_entries and tt <= cutoff:
                pending, sig_time = round(low - 1, 2), hm          # replace with new signal
            else:
                pending = sig_time = None                         # no fresh signal -> cancel
            continue

        # ── flat, no resting limit: a fresh signal arms one ───────────────────
        if entries < max_entries and tt <= cutoff and is_red and below:
            pending, sig_time = round(low - 1, 2), hm

    return events


def mtm_series(combined: pd.DataFrame, events: list[dict],
               lot_size: int = 1, lots: int = 1) -> list[dict]:
    """Running intraday MTM at every candle close: realized (closed cycles) plus
    unrealized of any open position, marked to that candle's close.
    Each point: {time, points, rupees}."""
    pairs, open_e = [], None
    for e in events:
        if e["type"] == "entry":
            open_e = e
        elif e["type"] == "exit" and open_e is not None:
            pairs.append((open_e["time"], open_e["price"], e["time"], e["price"]))
            open_e = None
    open_entry = (open_e["time"], open_e["price"]) if open_e else None

    out = []
    for _, r in combined.iterrows():
        hm = r["datetime"].strftime("%H:%M")
        close = float(r["close"])
        realized = sum((ep - xp) for (et, ep, xt, xp) in pairs if xt <= hm)
        unreal = 0.0
        active = next(((ep) for (et, ep, xt, xp) in pairs if et <= hm < xt), None)
        if active is not None:
            unreal = active - close
        elif open_entry and open_entry[0] <= hm:
            unreal = open_entry[1] - close
        pts = round(realized + unreal, 2)
        out.append({"time": hm, "points": pts, "rupees": round(pts * lot_size * lots, 2)})
    return out


def pair_trades(events: list[dict], lot_size: int = 1, lots: int = 1) -> dict:
    """Pair entries with their exits and compute per-trade + net P&L.

    Returns {trades:[...], open_trade: {...}|None, net_points, net_pnl,
             realized_points, realized_pnl}.
    """
    trades: list[dict] = []
    open_trade = None
    open_e = None
    for e in events:
        if e["type"] == "entry":
            open_e = e
        elif e["type"] == "exit" and open_e is not None:
            pts = round(open_e["price"] - e["price"], 2)        # short P&L
            trades.append({
                "fill_no":     open_e["fill_no"],
                "entry_time":  open_e["time"], "entry_price": open_e["price"],
                "exit_time":   e["time"],      "exit_price":  e["price"],
                "points":      pts,
                "pnl":         round(pts * lot_size * lots, 2),
            })
            open_e = None
    if open_e is not None:        # entered but never exited (open at EOD)
        open_trade = {
            "fill_no": open_e["fill_no"], "entry_time": open_e["time"],
            "entry_price": open_e["price"], "exit_time": None, "exit_price": None,
            "points": None, "pnl": None, "open": True,
        }
    realized_points = round(sum(t["points"] for t in trades), 2)
    realized_pnl = round(sum(t["pnl"] for t in trades), 2)
    return {
        "trades": trades, "open_trade": open_trade,
        "realized_points": realized_points, "realized_pnl": realized_pnl,
        "net_points": realized_points, "net_pnl": realized_pnl,
        "lot_size": lot_size, "lots": lots,
    }
