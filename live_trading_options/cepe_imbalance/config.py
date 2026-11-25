"""
cepe_imbalance/config.py — settings for the CE/PE imbalance PAPER engine.
==========================================================================

Kotak underlyings on ONE Kotak P market-feed connection (NSE F&O, BSE F&O, MCX) + BTC on the public Delta feed:
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

# Kotak NSE F&O and NSE commodity expiry epochs count from 1980-01-01 UTC (BSE / MCX from 1970).
EPOCH_1980 = 315532800

# Session hours (IST, "HH:MM"). MCX closes 23:55 while the US is on standard time
# (Nov -> Mar) and 23:30 during US daylight saving (Mar -> Nov). Editable here.
NSE_HOURS = ("09:15", "15:30")
MCX_HOURS_US_STD = ("09:00", "23:55")
MCX_HOURS_US_DST = ("09:00", "23:30")

# Square-off this many minutes before the session close (strategy.on_session_end).
SQUARE_OFF_BEFORE_MIN = {"index": 15, "commodity": 10, "crypto": 10}

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
    # NSE commodity segment (owner 9-Nov): WTI crude, 100 bbl a lot, monthly options, same hours as MCX.
    "NSECRUDE": {"seg": "nse_com", "scrip": "CRUDEOIL", "spot": None,             "kind": "commodity", "label": "NSE Crude"},
    # Delta Exchange India BTC daily options (owner 14-Nov-2026), PAPER, priced in USD, public Delta feed
    # (crypto.py). Two versions on the same contracts: the Indian day (09:00 -> 17:20 IST on today's
    # expiry) and the full expiry (17:30 -> 17:20 next day on the daily that lists at 17:30).
    "BTC":      {"seg": "delta",   "scrip": "BTC",      "spot": "delta|.DEXBTUSD", "kind": "crypto",   "label": "BTC (IST 09:00)"},
    "BTCFULL":  {"seg": "delta",   "scrip": "BTC",      "spot": "delta|.DEXBTUSD", "kind": "crypto",   "label": "BTC (full expiry)"},
}
ORDER = tuple(UNDERLYINGS)
KOTAK_SEGS = tuple(sorted({s["seg"] for s in UNDERLYINGS.values() if s["seg"] != "delta"}))

# ── crypto (Delta Exchange India) ──────────────────────────────────────────
# Daily BTC options settle 17:30 IST (12:00 UTC); the next daily lists at that moment. Crypto trades every
# day, so these books auto-start on weekends too. P&L is in USD (shown as $, never added to the rupee total).
# session: open / close IST; "overnight" = opens the evening BEFORE its trading day (the full-expiry cycle).
CRYPTO_SESSION = {"BTC": {"open": "09:00", "close": "17:30", "overnight": False},
                  "BTCFULL": {"open": "17:30", "close": "17:30", "overnight": True}}
CRYPTO_SETTLE = "17:30"
DELTA_LOT = 1000             # contracts per paper lot (owner 14-Nov): 1000 x 0.001 BTC = 1 BTC, $1 per $1 premium
DELTA_SPOT = ".DEXBTUSD"


def kind_of(name: str) -> str:
    return UNDERLYINGS[und_of(name)]["kind"]


def ccy_of(name: str) -> str:
    return "USD" if kind_of(name) == "crypto" else "INR"

# Option chain kept live per underlying: this many strikes each side of ATM, on these expiries
# (0 = nearest, 1 = next). Re-centred as ATM moves; old strikes stay subscribed (cheap).
STRIKES_EACH_SIDE = 15
EXPIRIES_LIVE = 2

# Strategy strike grid (owner, 3-Nov-2026). Kotak lists CRUDEOIL every 50; we trade every 200.
STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100, "CRUDEOIL": 200, "GOLDM": 500, "GOLD": 500,
               "SILVERM": 1000, "SILVER": 1000, "NSECRUDE": 200, "BTC": 200, "BTCFULL": 200}
GRID_EACH_SIDE = 12          # grid strikes kept subscribed each side of ATM (prices ready before a roll)

# MTM / imbalance history sampling (seconds) -> data/history/{date}/{name}.jsonl
HISTORY_EVERY_S = 0.5

# Auto-start (owner, 3-Nov-2026): each instrument starts itself once a day at this IST time, Mon-Fri,
# inside its session. Toggle / time per instrument on the screen (saved in data/settings.json).
# A manual Start or Stop that day ends auto-start for that day.
AUTO_START_DEFAULT = True
AUTO_START_TIME = {"index": "09:20", "commodity": "09:05", "BTC": "09:00", "BTCFULL": "17:31"}

# Supertrend VARIANT (owner 13-Nov-2026): a second paper book per instrument, named "{NAME}_ST", that
# trades the same rules but only while Supertrend on the 1-min ATM CE+PE candles is red (see st_signal.py).
# It is armed just after the market's first 1-min candle closes, so the opening low-break rule can act.
VARIANT_SUFFIX = "_ST"
ST_AUTO_START_TIME = {"index": "09:16", "commodity": "09:01", "BTC": "09:01", "BTCFULL": "17:31"}
ST_WARMUP_MIN = 150          # minutes of the previous session replayed to warm Supertrend up
QUOTES_FLUSH_WAIT_S = 3      # the quote recorder writes every 2 s


# NEW-RULES book (owner 15-Nov-2026): "{NAME}_NR" = strategy CePeNewRules (breach-only, heavy front strike ->
# behind the last, B only as fallback), band 30 % / step 400. Owner 16-Nov: both BTC books, and each also as
# a Supertrend book "{NAME}_NR_ST" (Supertrend on/off + the new adjustment rules).
NR_SUFFIX = "_NR"
NR_BOOKS = ("BTC", "BTCFULL")


# LIVE books (owner 16-Nov-2026): "{NAME}_LIVE" = the NORMAL rules with REAL orders on Kotak D (= Kotak P,
# env *_D, the same login as this engine's feed). Own book from own fills, 1 lot, own start / stop / lots /
# max loss / settings per index. Trades only while the owner has ARMED it (data/live/control.json).
LIVE_SUFFIX = "_LIVE"
LIVE_BOOKS = ("NIFTY", "SENSEX")
LIVE_DEFAULT_PARAMS = {"lots": 1, "mtm_stop": 5000.0}       # owner 16-Nov: 1 lot, max loss Rs 5,000 per index
LIVE_AUTO_TIME = "09:16"                                    # owner 16-Nov: auto-start 09:16, only when ARMED
LIVE_AUTO_DEFAULT = True
LIVE_MAX_LOTS = 10                                          # typo guard on the live lots box
LIVE_TAG = "cpek"                                           # Kotak order-tag prefix (unique tag per order)


# FLEX rules (owner 16/17-Nov-2026): NIFTY / SENSEX normal, Supertrend and LIVE books trade strategy CePeFlex
# (>= 2 strikes a side, heavy shift before B, flexible lots, one side keeps ATM, no C, planned ahead).
FLEX_BOOKS = ("NIFTY", "SENSEX")


def is_flex(name: str) -> bool:
    return und_of(name) in FLEX_BOOKS and not is_new_rules(name) and not is_syn(name)


# SYNTHETIC TO-AND-FRO (owner 23-Nov-2026): "{NAME}_SYN" paper + "{NAME}_SYN_LIVE" live (Kotak P), strategy
# SynToFro (synthetic.py). Moves with the synthetic price ATM + CE - PE (LTP): an ITM short is pushed OUT past
# the last sold strike, an OTM short pulled IN to ATM, to and fro. The CE/PE imbalance is shown, never traded on.
# Shown on the MCX terminal's "Algo" tab, not on the CE/PE page.
SYN_SUFFIX = "_SYN"
SYN_BOOKS = ("NIFTY", "SENSEX")
SYN_AUTO_TIME = "09:20"


SYN2_SUFFIX = "_SYN2"        # owner 25-Nov-2026: "Premium" = V2 (option-price triggers), same books otherwise


def syn_version(name: str) -> int:
    """0 = not a synthetic book, 1 = Synthetic (V1, "_SYN"), 2 = Premium (V2, "_SYN2")."""
    if name.endswith(LIVE_SUFFIX):
        name = name[:-len(LIVE_SUFFIX)]
    return 2 if name.endswith(SYN2_SUFFIX) else 1 if name.endswith(SYN_SUFFIX) else 0


def is_syn(name: str) -> bool:
    return syn_version(name) > 0


# SEPARATE LIVE ENGINE (owner 17-Nov-2026): the same program runs as two processes.
#   CEPE_ROLE=paper  port 47690: every paper book + the page / API; requests for *_LIVE books are forwarded
#                    to the live engine (so the screen is unchanged)
#   CEPE_ROLE=live   port 47691: ONLY the *_LIVE books, own Kotak login + own small feed (only the index that
#                    is armed or holds positions), own settings file, own scrip cache, no quote recorder
#   unset ("all")    old single process (tests, tools)
import os as _os
ROLE = _os.getenv("CEPE_ROLE", "all").strip().lower() or "all"
LIVE_PORT = 47691
LIVE_SEGS = ("nse_fo", "bse_fo")                            # NIFTY / SENSEX options only


def port_for(role: str | None = None) -> int:
    return LIVE_PORT if (role or ROLE) == "live" else PORT


def book_in_role(name: str, role: str | None = None) -> bool:
    role = role or ROLE
    if role == "paper":
        return not is_live(name)
    if role == "live":
        return is_live(name)
    return True


def is_live(name: str) -> bool:
    return name.endswith(LIVE_SUFFIX)


def und_of(name: str) -> str:
    """Book name -> underlying ("NIFTY_ST" -> "NIFTY", "BTC_NR" -> "BTC", "BTC_NR_ST" -> "BTC", "NIFTY_LIVE" -> "NIFTY")."""
    if name.endswith(LIVE_SUFFIX):
        name = name[:-len(LIVE_SUFFIX)]
    for suf in (SYN2_SUFFIX, SYN_SUFFIX):
        if name.endswith(suf):
            return name[:-len(suf)]
    if name.endswith(VARIANT_SUFFIX):
        name = name[:-len(VARIANT_SUFFIX)]
    if name.endswith(NR_SUFFIX):
        name = name[:-len(NR_SUFFIX)]
    return name


def is_new_rules(name: str) -> bool:
    if name.endswith(VARIANT_SUFFIX):
        name = name[:-len(VARIANT_SUFFIX)]
    return name.endswith(NR_SUFFIX)


def is_variant(name: str) -> bool:
    return name.endswith(VARIANT_SUFFIX)


def label_of(name: str) -> str:
    return UNDERLYINGS[und_of(name)]["label"] + (" · Premium" if syn_version(name) == 2 else " · Synthetic" if is_syn(name) else "") + (" · Supertrend" if is_variant(name) else "") +         (" · New rules" if is_new_rules(name) else "") + (" · LIVE Kotak D" if is_live(name) else "")


ALL_BOOKS = ORDER + tuple(n + VARIANT_SUFFIX for n in ORDER) + tuple(n + NR_SUFFIX for n in NR_BOOKS)     + tuple(n + NR_SUFFIX + VARIANT_SUFFIX for n in NR_BOOKS) + tuple(n + LIVE_SUFFIX for n in LIVE_BOOKS) \
    + tuple(n + SYN_SUFFIX for n in SYN_BOOKS) + tuple(n + SYN_SUFFIX + LIVE_SUFFIX for n in SYN_BOOKS)     + tuple(n + SYN2_SUFFIX for n in SYN_BOOKS) + tuple(n + SYN2_SUFFIX + LIVE_SUFFIX for n in SYN_BOOKS)
