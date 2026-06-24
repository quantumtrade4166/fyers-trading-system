"""
strangle_system/config.py
=========================
Single source of truth for every tunable, threshold, and path in the system.
Engineering rule (build spec §8): NO threshold or weight may be hard-coded in
layer logic — it lives here, so the whole system can be re-tuned in one place.

This file is intentionally plain data + light helpers. It must not import any
layer module (avoid cycles).
"""

import sys
from pathlib import Path

# Repo root = parent of strangle_system/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

# Where daily option-chain snapshots live (one parquet per underlying per day).
#   data/chain_snapshots/{UNDERLYING}/{YYYY-MM-DD}.parquet
CHAIN_SNAPSHOT_DIR = DATA_DIR / "chain_snapshots"
CHAIN_MANIFEST_FILE = CHAIN_SNAPSHOT_DIR / "chain_manifest.json"

# Daily spot OHLC for underlyings whose 5-min history isn't in the main data
# tree (e.g. SENSEX/BSE). Used by L1 realized-vol when 5-min isn't available.
#   data/spot_daily/{UNDERLYING}.parquet
SPOT_DAILY_DIR = DATA_DIR / "spot_daily"

# Annualization — Indian markets ~252 trading days.
TRADING_DAYS_PER_YEAR = 252


# ──────────────────────────────────────────────────────────────────────────
# UNDERLYINGS
# ──────────────────────────────────────────────────────────────────────────
# strike_step  : strike interval for ATM rounding / wall logic.
# index_symbol : Fyers spot symbol used for the optionchain call + RV.
# expiry_weekday: 0=Mon ... 6=Sun. NIFTY weeklies = Tuesday (1); Sensex = Thursday (3).
#                 (Lot sizes are read live from the Fyers symbol master at build time,
#                  never hard-coded — left None here on purpose.)
UNDERLYINGS = {
    "NIFTY": {
        "index_symbol": "NSE:NIFTY50-INDEX",
        "strike_step": 50,
        "expiry_weekday": 1,        # Tuesday (NSE shifted from Thursday)
        "exchange": "NSE",
        "lot_size": None,           # resolved from symbol master
    },
    "SENSEX": {
        "index_symbol": "BSE:SENSEX-INDEX",
        "strike_step": 100,
        "expiry_weekday": 3,        # Thursday
        "exchange": "BSE",
        "lot_size": None,           # resolved from symbol master
    },
}

# Which underlyings the collector captures by default (v1 = both per user).
ACTIVE_UNDERLYINGS = ["NIFTY", "SENSEX"]


# ──────────────────────────────────────────────────────────────────────────
# CHAIN COLLECTOR
# ──────────────────────────────────────────────────────────────────────────
# strikecount: passed to Fyers optionchain. N gives ATM + N ITM + N OTM per side.
#   Must be wide enough to capture Call/Put walls for GEX (L4), so kept generous.
CHAIN_STRIKECOUNT = 25

# How many of the nearest expiries to snapshot (term structure / L3 needs ≥2).
CHAIN_NUM_EXPIRIES = 3

# Always request greeks (delta, gamma, theta, vega, iv) — gives real IV for L1
# and real per-strike gamma for L4 without Black-Scholes inversion.
CHAIN_GREEKS = 1

# Multi-capture insurance: the collector is scheduled to run at each of these
# IST times (market still live → real bid/ask + near-final OI). Latest
# successful capture of the day wins. A crash only loses the day if it spans
# the entire window. (Scheduling is external — Task Scheduler on the VPS.)
CHAIN_CAPTURE_TIMES_IST = ["15:05", "15:15", "15:25"]

# Seconds between Fyers calls (mirror existing pipeline rate-limit discipline).
SLEEP_BETWEEN_CALLS = 0.5

# Push each saved snapshot to Google Drive via service account (headless,
# self-confirming). Set False to disable (e.g. local dev without creds).
CHAIN_PUSH_TO_DRIVE = True
CHAIN_DRIVE_FOLDER = "StrangleSystem_ChainSnapshots"


# ──────────────────────────────────────────────────────────────────────────
# LAYER 1 — VOLATILITY EDGE  (consumed once L1 is built)
# ──────────────────────────────────────────────────────────────────────────
RV_LOOKBACKS = [5, 10, 20]          # trading-day windows for realized vol
RV_DEFAULT_ESTIMATOR = "yang_zhang"  # handles overnight gaps; preferred default
EWMA_LAMBDA = 0.94                   # RiskMetrics daily lambda
IV_RANK_WINDOW = 252                 # trailing days for IV rank / percentile

# VRP = ATM_IV − forecast_RV (annualized, same horizon). Favorable to sell when
# VRP is comfortably positive; default to no-trade when not.
VRP_MIN_FAVORABLE = 0.0              # placeholder; tuned via backtest, not guessed


# ──────────────────────────────────────────────────────────────────────────
# LAYER 2 — EVENT & REGIME GUARDRAILS
# ──────────────────────────────────────────────────────────────────────────
# Manual event calendar the user maintains. CSV columns: date,event,severity
#   severity ∈ {high, medium, low}; "high" → hard veto, "medium" → size-down.
EVENT_CALENDAR_FILE = PROJECT_ROOT / "strangle_system" / "data" / "event_calendar.csv"

# Trend regime via ADX + EMA stack on daily spot.
ADX_PERIOD = 14
ADX_RANGE_MAX = 20.0     # adx < this → RANGE (friendly to short strangles)
ADX_TREND_MIN = 25.0     # adx ≥ this → STRONG_TREND (hostile)
EMA_STACK = [20, 50]     # alignment check

# Expiry-day handling: selling on expiry day is extreme gamma risk.
EXPIRY_DAY_VETO = True

# v1 decision flag (Layers 1+2 only). The full weighted L5 score replaces this
# in Phase 5; this is a deployable interim. Thresholds are PLACEHOLDERS pending
# the VRP-validation backtest — not tuned values.
V1_VRP_TRADE_MIN = 0.02          # require ≥2 vol-points of VRP to sell
V1_VETO_STRONG_TREND = True      # STRONG_TREND → no-trade in v1

# Decision output + paper log (the morning flag and its audit trail).
FLAG_DIR = PROJECT_ROOT / "strangle_system" / "flags"
PAPER_LOG_FILE = FLAG_DIR / "paper_signal_log.csv"


# ──────────────────────────────────────────────────────────────────────────
# LAYER 3 — TERM STRUCTURE & SKEW
# ──────────────────────────────────────────────────────────────────────────
# Term structure: far ATM IV − near ATM IV (annualized decimal vol points).
#   slope > +thresh → CONTANGO (calm, supportive); < −thresh → BACKWARDATION.
TERM_STATE_THRESHOLD = 0.005          # 0.5 vol points

# Skew: 25-delta put IV − 25-delta call IV (decimal vol points).
#   > +thresh → PUT_SKEW (puts richer, typical index); < −thresh → CALL_SKEW.
SKEW_DELTA = 0.25
SKEW_STATE_THRESHOLD = 0.005          # 0.5 vol points


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────
def reconfigure_stdout():
    """Project-wide Windows UTF-8 fix (see CLAUDE.md)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
