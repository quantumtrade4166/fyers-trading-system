"""
strategy_builder/config.py — the few constants the builder needs.

Lot sizes / strike steps are the ones the live strangle uses
(live_trading_options/strangle_strategy/config/parameters.json, confirmed from the
Fyers symbol master). Read from there so the two systems can never disagree.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]                 # G:\fyers_data_pipeline
PKG = Path(__file__).resolve().parent
DATA = PKG / "data"
STRATEGIES_FILE = DATA / "strategies.json"
LOG_DIR = ROOT / "logs"

PORT = 47674          # builder service (also its single-instance lock: one bind per port)

_STRANGLE_PARAMS = ROOT / "live_trading_options" / "strangle_strategy" / "config" / "parameters.json"
try:
    _p = json.loads(_STRANGLE_PARAMS.read_text(encoding="utf-8"))
    LOT_SIZE = dict(_p["lot_sizes"])
    STRIKE_STEP = dict(_p["strike_interval"])
except Exception:                                          # never block the chart on a config read
    LOT_SIZE = {"NIFTY": 65, "SENSEX": 20}
    STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100}

INDEX_SYMBOL = {"NIFTY": "NSE:NIFTY50-INDEX", "SENSEX": "BSE:SENSEX-INDEX"}
VIX_SYMBOL = "NSE:INDIAVIX-INDEX"
INDICES = ("NIFTY", "SENSEX")

STRIKECOUNT = 50          # strikes each side of ATM from Fyers optionchain (50 is its max)
# Fyers REST is capped at 5,000 requests/day for the WHOLE account (strangle engine, data tasks and
# this builder share it — hit on 2026-10-24). So the builder pulls the chain only while a page is
# actually looking, once a minute (prices between pulls stream over the websocket, which is free),
# and never more than DAILY_REST_CAP times a day.
REST_REFRESH_S = 60       # full chain re-pull (OI, new strikes) per watched expiry
WATCH_TTL_S = 120         # no page has asked for this expiry for 2 min -> stop pulling it
DAILY_REST_CAP = 1200     # builder's own ceiling, well inside the account's 5,000
EXPIRY_HOUR = (15, 30)    # options expire 15:30 IST
