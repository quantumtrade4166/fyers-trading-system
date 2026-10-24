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
}
