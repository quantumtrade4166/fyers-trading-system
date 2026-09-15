"""
Indian index-option transaction cost model (NSE, NIFTY weekly options).

Charges are applied to PREMIUM turnover (premium x quantity), per side:

  Brokerage     Rs 20 per executed order (orders split at the exchange
                freeze quantity, so big positions pay per slice)
  STT           sell side only, on premium. Rate changed over the backtest:
                  0.05%   before 2023-04-01
                  0.0625% 2023-04-01 .. 2024-09-30
                  0.1%    from 2024-10-01
  Exchange txn  both sides. NSE options: 0.05% before 2024-10-01,
                0.03503% from 2024-10-01 (NSE "true-to-label" revision)
  SEBI fee      Rs 10 per crore = 0.0001%, both sides
  Stamp duty    0.003% on the BUY side only
  GST           18% on (brokerage + exchange txn + SEBI fee)

Rates are best-known values and are constants below -- verify against a
recent contract note, especially for any change after Oct 2024.
Exercise STT (0.125% on intrinsic) is not applicable: everything is squared
off intraday before expiry settlement.
"""
import math
import pandas as pd

BROKERAGE_PER_ORDER = 20.0
FREEZE_QTY = 1800            # NIFTY max qty per order; larger orders are sliced
SEBI_RATE = 0.000001
STAMP_RATE_BUY = 0.00003
GST_RATE = 0.18

STT_SCHEDULE = [             # (effective_from, sell-side rate on premium)
    ("2000-01-01", 0.0005),
    ("2023-04-01", 0.000625),
    ("2024-10-01", 0.001),
]
EXCH_SCHEDULE = [
    ("2000-01-01", 0.0005),
    ("2024-10-01", 0.0003503),
]


def _rate(schedule, date_str):
    rate = schedule[0][1]
    for eff, r in schedule:
        if date_str >= eff:
            rate = r
    return rate


def statutory_costs(sell_price, buy_price, qty, date_str):
    """Costs for one round trip of a naked short: SELL to open, BUY to close."""
    sell_turn = sell_price * qty
    buy_turn = buy_price * qty
    orders_per_side = math.ceil(qty / FREEZE_QTY)

    brokerage = BROKERAGE_PER_ORDER * orders_per_side * 2
    stt = sell_turn * _rate(STT_SCHEDULE, date_str)
    exch = (sell_turn + buy_turn) * _rate(EXCH_SCHEDULE, date_str)
    sebi = (sell_turn + buy_turn) * SEBI_RATE
    stamp = buy_turn * STAMP_RATE_BUY
    gst = (brokerage + exch + sebi) * GST_RATE
    return {"brokerage": brokerage, "stt": stt, "exch": exch, "sebi": sebi,
            "stamp": stamp, "gst": gst,
            "total": brokerage + stt + exch + sebi + stamp + gst}
