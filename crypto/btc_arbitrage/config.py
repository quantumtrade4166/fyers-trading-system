"""
config.py — every tunable of the BTC cross-exchange spread monitor, in one place.
================================================================================

Nothing else in this package hard-codes a symbol, a fee, a threshold or an
endpoint. Change a value here and restart the monitor (run_monitor.bat / the
BtcArbMonitor task); the dashboard picks the new values up from the published
tick, so it never needs editing for a config change.

READ-ONLY BY DESIGN: this monitor uses public market-data endpoints only. There
is no API key anywhere in this package and no code path that can place, cancel
or modify an order.

Verified against the live exchanges on 2026-10-03 (see README "Contracts"):

  Delta Exchange India   BTCUSD     perpetual   0.001 BTC / contract   USD
                         (the ONLY BTC future Delta India lists — no dated futures)
  Binance USD-M          BTCUSDT    perpetual   qty already in BTC     USDT
  Binance USD-M          BTCUSDC    perpetual   qty already in BTC     USDC
  Binance COIN-M         BTCUSD_PERP perpetual  $100 / contract        USD (inverse)

So the economically equivalent pair is perpetual vs perpetual. Binance's dated
quarterlies (BTCUSDT_261225 …) have no Delta counterpart and are refused.
"""

from pathlib import Path

# ── contracts ────────────────────────────────────────────────────────────
DELTA_SYMBOL = "BTCUSD"

# Which Binance perpetual to compare against. Must be a key of BINANCE_CONTRACTS.
BINANCE_SYMBOL = "BTCUSDT"

# The mapping layer: how each Binance perpetual is quoted and sized. The
# monitor checks these against Binance's exchangeInfo at start-up and refuses
# to run if the exchange disagrees (wrong contract type, wrong base asset).
BINANCE_CONTRACTS = {
    "BTCUSDT":     {"market": "usdm",  "quote": "USDT", "qty_unit": "btc"},
    "BTCUSDC":     {"market": "usdm",  "quote": "USDC", "qty_unit": "btc"},
    # COIN-M is INVERSE (margined and settled in BTC). Its price is a real USD
    # price, but the P&L of a hedge against Delta's linear contract is not
    # 1:1 — fine for watching the spread, think twice before trading it.
    "BTCUSD_PERP": {"market": "coinm", "quote": "USD",  "qty_unit": "usd_100"},
}

DELTA_CONTRACT = {"contract_type": "perpetual_futures", "underlying": "BTC",
                  "contract_value_btc": 0.001, "quote": "USD"}

# Binance's USDT perpetual is priced in USDT, Delta in USD. USDT trades a few
# basis points off USD, and 1 bp is ~$8 on BTC — the same size as the spread
# we are looking at. When True, Binance prices are converted to USD using the
# live USDC/USDT rate (USDC ~ USD). Default False: compare raw prices, exactly
# as the exchanges quote them, and show the rate on the dashboard instead.
ADJUST_QUOTE_TO_USD = False

# ── fees (fractions, not percent) ────────────────────────────────────────
# Taker rates — the monitor assumes both legs cross the spread.
DELTA_TAKER_FEE = 0.0005        # 0.05%  (Delta /v2/products reports this live; a
                                #         mismatch is logged at start-up)
DELTA_FEE_GST = 0.18            # India: 18% GST charged on top of Delta's fee
BINANCE_TAKER_FEE = 0.0005      # 0.05%  USD-M VIP0; set 0.00045 with the BNB discount

# "round_trip" = open both legs AND close both legs (4 fills) — the honest cost
# of a spread trade. "entry" = only the two opening fills.
FEE_MODE = "round_trip"

# ── size / depth ─────────────────────────────────────────────────────────
TRADE_SIZE_BTC = 0.01           # v1: checks top-of-book size covers this
                                # (core/depth.py holds the L2 walk for later)

# ── data quality ─────────────────────────────────────────────────────────
MAX_DATA_AGE_MS = 3000          # either quote older than this -> STALE, no opportunity
MAX_QUOTE_TIME_DIFF_MS = 1500   # the two quotes received further apart than this
                                # -> not a simultaneous picture, no opportunity
STALE_SOCKET_S = 30             # no message at all for this long -> rebuild socket

# ── opportunity ──────────────────────────────────────────────────────────
# A direction is an OPPORTUNITY only when its spread AFTER FEES clears BOTH
# thresholds and has done so continuously for OPPORTUNITY_MIN_MS.
OPPORTUNITY_THRESHOLD_USD = 5.0       # per 1 BTC
OPPORTUNITY_THRESHOLD_PCT = 0.005     # percent of price (0.005% ~ $4 at $84k)
OPPORTUNITY_MIN_MS = 500              # filters one-message blips from async updates
# A running opportunity is re-logged only when its net spread moves this much.
OPPORTUNITY_CHANGE_USD = 10.0

# ── paper trading (zero fees, depth-priced) ──────────────────────────────
# Switched on/off and tuned LIVE from the dashboard tab; these are only the
# size and file locations. Entry / exit / latency defaults live in core/paper.py
# (DEFAULTS) and are overridden by data/state/paper_control.json.
PAPER_SIZE_BTC = 1.0            # each side, priced by walking both order books

# ── chart / storage ──────────────────────────────────────────────────────
CHART_WINDOWS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
CHART_MAX_POINTS = 1500         # the API downsamples any window to at most this
CHART_HISTORY_MINUTES = 60 * 24 # how far back the dashboard may ask
RETENTION_DAYS = 90             # daily SQLite files older than this are deleted

TICK_WRITE_MS = 250             # how often the live tick file is republished
FUNDING_POLL_S = 60             # Binance funding (REST); Delta funding is pushed
CLOCK_SYNC_S = 300              # re-measure the local clock offset this often

# ── endpoints (verified 2026-10-03) ──────────────────────────────────────
DELTA_WS = "wss://socket.india.delta.exchange"
DELTA_REST = "https://api.india.delta.exchange"
BINANCE_ENDPOINTS = {
    "usdm":  {"ws": "wss://fstream.binance.com/ws", "rest": "https://fapi.binance.com/fapi/v1"},
    "coinm": {"ws": "wss://dstream.binance.com/ws", "rest": "https://dapi.binance.com/dapi/v1"},
}
BINANCE_SPOT_REST = "https://api.binance.com/api/v3"
USER_AGENT = "fyers-pipeline/btc-arbitrage-monitor"

# ── paths / process ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB_DIR = DATA / "spread_db"           # one SQLite file per IST day
STATE = DATA / "state"
TICK_FILE = STATE / "TICK.json"
EVENTS_FILE = STATE / "events.jsonl"  # dashboard event feed (opportunities + connection)
LOGS = ROOT / "logs"
APP_LOG = LOGS / "application.log"
OPPORTUNITIES_CSV = LOGS / "opportunities.csv"

PAPER_DIR = DATA / "paper"
PAPER_CONTROL = STATE / "paper_control.json"   # written by the dashboard toggle
PAPER_STATE = PAPER_DIR / "paper_state.json"   # open position survives a restart
PAPER_TRADES = PAPER_DIR / "trades.jsonl"      # one line per closed paper trade

LOCK_PORT = 47664                     # single-instance guard (see README)
