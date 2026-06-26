"""
core/signal_engine.py
=====================

Strategy entry/exit logic over combined-premium 5-min candles.

This is the historical/backtest simulation of the live rules — it turns the raw
"candle is below VWAP" conditions into the actual sequence of trades the strategy
would take, so charts show a handful of real entries/exits (not dozens of raw
signal candles).

Rules (from the spec):
  - Strikes are fixed at 9:20; trading starts from the 9:20 candle onward.
  - ENTRY  : a 5-min candle closes BELOW VWAP and is RED (close < open)
             -> sell the strangle (limit at candle_low - 1). Counts as a fill.
  - Max 4 filled entries per day (scale-ins allowed); once consumed, never reset.
  - EXIT   : any candle closes ABOVE VWAP -> buy back all open legs at market.
             Exits do not restore the entry count.

This is a 5-min-close approximation (fills assumed at low-1). The live engine
will use ticks + real order fills; this shared module documents the canonical
rule set.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd

MAX_ENTRIES_PER_DAY = 4


def simulate_day(combined: pd.DataFrame, max_entries: int = MAX_ENTRIES_PER_DAY) -> list[dict]:
    """Return the ordered list of trade events for one day.

    Each event: {time:'HH:MM', type:'entry'|'exit', price:float,
                 fill_no:int (entries only), reason:str}
    """
    events: list[dict] = []
    entries = 0
    open_pos = False

    # skip the 9:15 candle — that is the strike-selection trigger, not a trade bar
    for _, r in combined.iloc[1:].iterrows():
        t = r["datetime"].strftime("%H:%M")
        if open_pos and bool(r["above_vwap"]):
            events.append({"time": t, "type": "exit",
                           "price": round(float(r["close"]), 2),
                           "reason": "close above VWAP"})
            open_pos = False
        elif entries < max_entries and bool(r["below_vwap"]) and bool(r["is_red"]):
            entries += 1
            events.append({"time": t, "type": "entry",
                           "price": round(float(r["low"]) - 1, 2),
                           "fill_no": entries, "reason": "red close below VWAP"})
            open_pos = True

    return events


def day_summary(combined: pd.DataFrame, events: list[dict]) -> dict:
    """Light P&L summary of the simulated day (per 1 lot of the combined premium,
    qty applied later). Sells then buys back; leftover open position marked to
    the last close."""
    realized = 0.0
    open_price = None
    n_entries = sum(e["type"] == "entry" for e in events)
    for e in events:
        if e["type"] == "entry":
            open_price = e["price"] if open_price is None else open_price  # avg ignored (viz)
        elif e["type"] == "exit" and open_price is not None:
            realized += (open_price - e["price"])   # short: sell high, buy low
            open_price = None
    last_close = float(combined.iloc[-1]["close"]) if len(combined) else None
    unrealized = (open_price - last_close) if (open_price is not None and last_close) else 0.0
    return {"entries": n_entries,
            "exits": sum(e["type"] == "exit" for e in events),
            "realized_points": round(realized, 2),
            "open_at_eod": open_price is not None,
            "unrealized_points": round(unrealized, 2)}
