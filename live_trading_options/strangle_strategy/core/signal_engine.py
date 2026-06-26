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

import pandas as pd

MAX_ENTRIES_PER_DAY = 4


def simulate_day(combined: pd.DataFrame, max_entries: int = MAX_ENTRIES_PER_DAY) -> list[dict]:
    """Ordered entry/exit events, strictly alternating (one position at a time).

    Each event: {time, type:'entry'|'exit', price, fill_no (entries), reason}
    """
    events: list[dict] = []
    entries = 0
    in_pos = False

    # skip the 9:15 candle — that is the strike-selection trigger, not a trade bar
    for _, r in combined.iloc[1:].iterrows():
        t = r["datetime"].strftime("%H:%M")
        if not in_pos and entries < max_entries and bool(r["below_vwap"]) and bool(r["is_red"]):
            entries += 1
            events.append({"time": t, "type": "entry",
                           "price": round(float(r["low"]) - 1, 2),
                           "fill_no": entries, "reason": "red close below VWAP"})
            in_pos = True
        elif in_pos and bool(r["above_vwap"]):
            events.append({"time": t, "type": "exit",
                           "price": round(float(r["close"]), 2),
                           "reason": "close above VWAP"})
            in_pos = False

    return events


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
