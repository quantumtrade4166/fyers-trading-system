"""
cepe_imbalance/config.py — settings for the CE/PE imbalance PAPER engine.
==========================================================================

Seven underlyings on ONE Kotak P market-feed connection (NSE F&O, BSE F&O, MCX):
  NIFTY, SENSEX (index options, ATM from index SPOT) and CRUDEOIL, GOLDM, GOLD, SILVERM, SILVER
  (options on futures, ATM from the option's underlying FUTURE).

Paper only: this package never sends an order to any broker.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]                  # ...\fyers_data_pipeline
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"                                        # scrip cache, books, trades, MTM history
LOG_DIR = ROOT / "logs"

HOST = "127.0.0.1"
PORT = 47690                 # service port + single-instance lock (one bind per port)
ENV_SUFFIX = "_D"            # Kotak P = env keys KOTAK_*_D (same login as the MCX terminal)

# Kotak feed limits: 3000 tokens per connection; keep headroom.
SUB_BUDGET = 2800
SUB_CHUNK = 400

# Kotak NSE/BSE F&O expiry epochs count from 1980-01-01 UTC (MCX counts from 1970).
EPOCH_1980 = 315532800

# Session hours (IST, "HH:MM"). MCX closes 23:55 while the US is on standard time
# (Nov -> Mar) and 23:30 during US daylight saving (Mar -> Nov). Editable here.
NSE_HOURS = ("09:15", "15:30")
MCX_HOURS_US_STD = ("09:00", "23:55")
MCX_HOURS_US_DST = ("09:00", "23:30")

# Square-off this many minutes before the session close (strategy.on_session_end).
SQUARE_OFF_BEFORE_MIN = {"index": 15, "commodity": 10}

# name -> how to find it.
#   seg        Kotak exchange segment of the options
#   scrip      exact pSymbolName in the scrip master
#   spot       feed key of the index spot (None = use the option's underlying future)
#   kind       "index" | "commodity"
UNDERLYINGS = {
    "NIFTY":    {"seg": "nse_fo", "scrip": "NIFTY",    "spot": "nse_cm|Nifty 50", "kind": "index",     "label": "Nifty"},
    "SENSEX":   {"seg": "bse_fo", "scrip": "SENSEX",   "spot": "bse_cm|SENSEX",   "kind": "index",     "label": "Sensex"},
    "CRUDEOIL": {"seg": "mcx_fo", "scrip": "CRUDEOIL", "spot": None,              "kind": "commodity", "label": "Crude Oil"},
    "GOLDM":    {"seg": "mcx_fo", "scrip": "GOLDM",    "spot": None,              "kind": "commodity", "label": "Gold Mini"},
    "GOLD":     {"seg": "mcx_fo", "scrip": "GOLD",     "spot": None,              "kind": "commodity", "label": "Gold"},
    "SILVERM":  {"seg": "mcx_fo", "scrip": "SILVERM",  "spot": None,              "kind": "commodity", "label": "Silver Mini"},
    "SILVER":   {"seg": "mcx_fo", "scrip": "SILVER",   "spot": None,              "kind": "commodity", "label": "Silver"},
}
ORDER = tuple(UNDERLYINGS)

# Option chain kept live per underlying: this many strikes each side of ATM, on these expiries
# (0 = nearest, 1 = next). Re-centred as ATM moves; old strikes stay subscribed (cheap).
STRIKES_EACH_SIDE = 15
EXPIRIES_LIVE = 2

# MTM / imbalance history sampling (seconds) -> data/history/{date}/{name}.jsonl
HISTORY_EVERY_S = 1.0
