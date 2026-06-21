"""
StatArb.MR-first10 — Definitive config for all 10 live pairs.
Source of truth for the deployment dashboard and signal engine.
All values extracted from portfolio_pairs_summary_v2.csv (Session 10, 2026-06-20).

Each entry:
  name, sym_A, qty_A, sym_B, qty_B, lookback, entry_z, stop_z, annual_stop
  qty_A = n_lots × lot_size for Leg A (actual shares to trade)
  qty_B = n_lots × lot_size for Leg B (actual shares to trade)
"""

PAIRS = [
    # name                    sym_A         qty_A  lots_A  sym_B          qty_B  lots_B  lb    ez   sz    ann_stop
    ("TCS/INFY",              "TCS",          450,      3,  "INFY",          900,     3, 126, 2.0, 3.5,    58_000),
    ("BAJAJFINSV/BAJFINANCE", "BAJAJFINSV",  2000,      4,  "BAJFINANCE",   4125,    33, 166, 2.5, 3.0,   209_295),
    ("HDFCBANK/KOTAKBANK",    "HDFCBANK",    5500,     10,  "KOTAKBANK",   12400,    31, 113, 2.0, 4.0,   393_340),
    ("HINDUNILVR/DABUR",      "HINDUNILVR",  3000,     10,  "DABUR",       16250,    13,  67, 1.5, 4.0,   508_632),
    ("OBEROIRLTY/BRIGADE",    "OBEROIRLTY",  7200,      9,  "BRIGADE",     14000,    14,  92, 1.5, 3.5, 1_137_471),
    ("TATAPOWER/JSWENERGY",   "TATAPOWER",  39600,      8,  "JSWENERGY",   26000,    13,  76, 2.0, 3.0, 1_211_814),
    ("TECHM/COFORGE",         "TECHM",       4800,      8,  "COFORGE",      3800,    19, 140, 1.5, 3.5,   501_534),
    ("EICHERMOT/TVSMOTORS",   "EICHERMOT",   1400,      7,  "TVSMOTOR",     2100,     6, 129, 1.5, 4.0,   554_023),
    ("HDFCLIFE/ICICIPRULI",   "HDFCLIFE",    3300,      3,  "ICICIPRULI",   3000,     2,  87, 2.0, 4.0,   223_222),
    ("SRF/DEEPAKNTR",         "SRF",         2500,     10,  "DEEPAKNTR",    2450,     7, 151, 2.0, 4.0, 1_052_772),
]

# Column index reference
NAME        = 0
SYM_A       = 1
QTY_A       = 2   # actual shares to trade for Leg A
LOTS_A      = 3   # number of lots for Leg A
SYM_B       = 4
QTY_B       = 5   # actual shares to trade for Leg B
LOTS_B      = 6   # number of lots for Leg B
LOOKBACK    = 7   # rolling OLS + z-score window (calendar days with data)
ENTRY_Z     = 8   # enter when |z| crosses this threshold
STOP_Z      = 9   # stop-loss when |z| crosses this threshold
ANNUAL_STOP = 10  # sit out rest of year if cumul. PnL < -this value

# Signal logic (identical for all pairs):
#   spread[t] = log(price_B[t]) - beta[t] * log(price_A[t])
#   beta[t]   = OLS slope of log(price_B) ~ log(price_A) over last LOOKBACK days
#   z[t]      = (spread[t] - mean(spread, LOOKBACK)) / std(spread, LOOKBACK)
#   +1 (long spread):  z < -ENTRY_Z  →  buy QTY_A of A, sell QTY_B of B
#   -1 (short spread): z > +ENTRY_Z  →  sell QTY_A of A, buy QTY_B of B
#   exit: z crosses 0
#   stop: |z| > STOP_Z
#   annual stop: cumul. PnL since Jan 1 < -ANNUAL_STOP → flat for rest of year

SPAN_FACTOR = 0.15   # SPAN margin = 15% of notional per leg
BROKERAGE   = 0.0003 # one-way brokerage rate used in backtest
EXIT_Z      = 0.5    # exit when |z| < 0.5 (not exact zero — matches backtest)
COOLDOWN    = 5      # bars of no-entry after any trade closes

# max_hl per pair: compute at startup using full price history (2015-2026)
#   ols_full = OLS(pa_all, add_constant(pb_all))   ← raw prices, NOT log
#   spread_full = pa_all - beta_full * pb_all
#   phi = AR(1) on diff(spread_full)
#   hl_full = -log(2) / log(1 + phi)
#   max_hl = hl_full * 2.0
# Entry is blocked if rolling half-life > max_hl for that bar
