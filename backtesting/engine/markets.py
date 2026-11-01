"""
Market profiles — one place for every market's session, costs and sizing,
so each book strategy runs on each market with the same honest assumptions.

cost_pct is charged PER SIDE on notional by the harness
(cost = cost_pct * (entry + exit) * units).
"""

MARKETS = {
    # NSE cash equities, intraday (MIS), 5-minute bars, 09:15–15:30 IST.
    "in_stocks_5m": {
        "intraday":       True,
        "bar_minutes":    5,
        "last_entry":     "14:55",   # no new trades after this bar
        "squareoff":      "15:15",   # bar that STARTS 15:15 closes 15:20 = broker MIS cut-off
        # Zerodha MIS: brokerage min(Rs20, 0.03%)/order, STT 0.025% sell,
        # exchange 0.00297%, stamp 0.003% buy, SEBI, GST on brokerage+exchange
        # ≈ 0.03% per side at Rs2–5L notional, + ~0.02% slippage per side.
        "cost_pct":       0.0005,
        "capital":        100_000.0,  # per-trade risk base (1% = Rs1,000 = 1R)
        "risk_pct":       0.01,
        "max_position_pct": 5.0,      # MIS ~5x leverage -> Rs5L notional cap
        "entry_on":       "next_open",
        "calendar":       "business",
        "oos_start":      "2025-10-01",  # in-sample before, out-of-sample from
        "loader":         "in_stocks_5m",
    },

    # NSE cash equities, DELIVERY (CNC), daily bars, Nifty 500 (current list).
    # ⚠️ Survivorship bias: today's Nifty 500 members, back to 2005.
    "in_stocks_1d": {
        "intraday":       False,
        # Delivery: STT 0.1% each side, stamp 0.015% buy, exchange ~0.003%,
        # DP ~Rs16 per sell, GST; ≈0.12% per side, + ~0.03% slippage.
        "cost_pct":       0.0015,
        "capital":        1_000_000.0,   # Rs10 lakh, 1% risk = Rs10,000 = 1R
        "risk_pct":       0.01,
        "max_position_pct": 0.20,        # no single stock > 20% of capital
        "entry_on":       "next_open",
        "calendar":       "business",
        "oos_start":      "2019-01-01",
        "loader":         "nifty500_1d",
    },

    # Same account rules as in_stocks_1d, but SURVIVORSHIP-FREE: every NSE
    # stock from the bhavcopy archive, tradable only while it is in that
    # day's top 500 by 63-day traded value (engine/bhav.py).
    "nse_all_1d": {
        "intraday":       False,
        "cost_pct":       0.0015,
        "capital":        1_000_000.0,
        "risk_pct":       0.01,
        "max_position_pct": 0.20,
        "entry_on":       "next_open",
        "calendar":       "business",
        "oos_start":      "2019-01-01",
        "loader":         "bhav_1d",
    },

    # NIFTY & BANK NIFTY index, 5-minute, traded as index FUTURES (book #1).
    "in_index_5m": {
        "intraday":       True,
        "bar_minutes":    5,
        "last_entry":     "14:55",
        "squareoff":      "15:15",
        # futures: Rs20/order brokerage, STT 0.02% on sell, exchange + GST,
        # ~1 index point slippage  ->  about 0.02% per side
        "cost_pct":       0.0002,
        "capital":        1_000_000.0,   # Rs10L, 1% = Rs10,000 = 1R
        "risk_pct":       0.01,
        "max_position_pct": 10.0,        # futures leverage cap
        "entry_on":       "next_open",
        "calendar":       "business",
        "oos_start":      "2025-09-01",
        "loader":         "in_index_5m",
        "levels":         True,          # engine/levels.add_levels
        "target_r":       2.0,           # the book's default "2R"
        "skip_if_stop_breached": True,
    },

}
MARKETS["in_index_5m"]["peer"] = {"NSE_NIFTY50_INDEX": "NSE_NIFTYBANK_INDEX",
                                   "NSE_NIFTYBANK_INDEX": "NSE_NIFTY50_INDEX"}

# NSE F&O stocks 5-min with the book-#1 machinery (levels, 2R target).
MARKETS["in_stocks_5m_lv"] = {**MARKETS["in_stocks_5m"], "levels": True, "target_r": 2.0,
                              "skip_if_stop_breached": True, "oos_start": "2025-09-01"}

# Bitcoin (Binance BTCUSDT perp proxy), 5-min, "day" = UTC calendar day.
# 24/7, so the session rules are only: no new entries after 23:00 UTC and
# square-off at the last bar of the UTC day (book #1 is an intraday method).
MARKETS["btc_5m"] = {
    "intraday": True, "bar_minutes": 5, "last_entry": "23:00", "squareoff": "23:55",
    "cost_pct": 0.0006, "funding_apr": 0.10, "capital": 1_000_000.0, "risk_pct": 0.01,
    "max_position_pct": 5.0, "entry_on": "next_open", "calendar": "calendar",
    "oos_start": "2023-01-01", "loader": "btc_5m", "levels": True, "target_r": 2.0,
    "skip_if_stop_breached": True,
}
