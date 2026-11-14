"""
mcx_terminal/config.py — settings for the order terminal (Kotak P).

ONE code base, TWO desks. The desk is picked when the engine starts, by the environment variable
TERMINAL_DESK (default MCX):
  MCX  MCX commodity options (CRUDEOIL, GOLD, GOLDM, SILVERM)     port 47680, data\\
  NSE  index options (NIFTY, BANKNIFTY on NSE, SENSEX on BSE)    port 47682, data_nse\\
       + NSE commodity crude oil (NSECRUDE, segment nse_com, owner 07-Nov-2026)
Each desk is its own process: own port, data folder, log, audit trail, Kotak session and order tags.

OWN_BOOK (NSE): the desk's LIVE positions are only the trades IT placed (matched by Kotak order id),
never the whole Kotak account - other strategies trade NIFTY on the same account and EXIT ALL must
never touch them (owner, 04-Nov-2026). MCX mirrors every MCX position on the account (owner, 28-Oct).
"""
import datetime as dt
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]                 # ...\fyers_data_pipeline
HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
LOG_DIR = ROOT / "logs"

DESK = os.getenv("TERMINAL_DESK", "MCX").strip().upper()
if DESK not in ("MCX", "NSE", "BTC"):
    raise SystemExit(f"TERMINAL_DESK must be MCX, NSE or BTC, not {DESK!r}")

# BTC desk (owner, 15-Nov-2026): Delta Exchange India BTC options, PAPER ONLY for now (live Delta orders are a
# later step - the one Delta account also runs the live BTC VWAP strategy). Prices from Delta's PUBLIC feed (no
# key needed). Money in USD. Delta trades 24/7, so the desk's "day" runs 17:35 -> 17:35 IST (after the daily
# options expire at 17:30) and is named by the date it ENDS on (= the date of that day's daily expiry).
BTC_SPOT_TOKEN = 1_000_000_001           # our own id for the BTC index .DEXBTUSD (Delta product ids are far below)

_DESKS = {
    "MCX": dict(
        PORT=47680, DATA=HERE / "data", LOG_NAME="mcx_terminal.log", AUDIT_NAME="mcx_audit",
        # NSEGOLD / NSEGOLDM = NSE's commodity GOLD / GOLDM (segment nse_com, owner 04-Nov-2026). Their own
        # product names: MCX already has GOLD and GOLDM, and the two must never be mixed up.
        INSTRUMENTS=("CRUDEOIL", "GOLD", "GOLDM", "SILVERM", "NSEGOLD", "NSEGOLDM"),
        LABELS={"CRUDEOIL": "Crude Oil", "GOLD": "Gold", "GOLDM": "Gold Mini", "SILVERM": "Silver Mini",
                "NSEGOLD": "NSE Gold", "NSEGOLDM": "NSE Gold Mini"},
        SEGMENT={"CRUDEOIL": "mcx_fo", "GOLD": "mcx_fo", "GOLDM": "mcx_fo", "SILVERM": "mcx_fo",
                 "NSEGOLD": "nse_com", "NSEGOLDM": "nse_com"},
        SYMBOL={"NSEGOLD": "GOLD", "NSEGOLDM": "GOLDM"},  # product -> Kotak pSymbolName (default: the same)
        SPOT={},                                           # MCX options are written on the future
        OWN_BOOK=False, TAG="mcxd", TAG_SL="mcxs",
        STRIKE_GAP={"CRUDEOIL": 50, "GOLD": 500, "GOLDM": 500, "SILVERM": 1000, "NSEGOLD": 500, "NSEGOLDM": 500},
        GAP_CHOICES={"CRUDEOIL": (50, 100, 200), "GOLD": (500, 1000, 2000), "GOLDM": (500, 1000, 2000),
                     "SILVERM": (1000, 2000, 3000, 4000, 5000),
                     "NSEGOLD": (100, 500, 1000, 2000), "NSEGOLDM": (100, 500, 1000, 2000)},
        REFRESH_AT=dt.time(8, 45),                         # fresh login + contract list before 09:00
        REPLAY_BUILD_AT=dt.time(23, 58),                   # after the 23:30 / 23:55 close
        HOURS={"open": "09:00", "close": "23:30", "winter_close": "23:55"},
    ),
    "NSE": dict(
        PORT=47682, DATA=HERE / "data_nse", LOG_NAME="nse_terminal.log", AUDIT_NAME="nse_audit",
        # NSECRUDE = NSE's commodity CRUDEOIL (segment nse_com: options on the future, 50-point strikes,
        # 1 lot = 100 barrels, checked on Kotak's list 07-Nov-2026). Own name: MCX has CRUDEOIL.
        INSTRUMENTS=("NIFTY", "BANKNIFTY", "SENSEX", "NSECRUDE"),
        LABELS={"NIFTY": "Nifty", "BANKNIFTY": "Bank Nifty", "SENSEX": "Sensex", "NSECRUDE": "NSE Crude"},
        SEGMENT={"NIFTY": "nse_fo", "BANKNIFTY": "nse_fo", "SENSEX": "bse_fo", "NSECRUDE": "nse_com"},
        SYMBOL={"NSECRUDE": "CRUDEOIL"},
        # index spot: (segment, feed token, label, REST quote name) - checked on Kotak 04-Nov-2026: the feed
        # takes the token; REST quotes() refuses the token but answers to the name (backup price source)
        SPOT={"NIFTY": ("nse_cm", 26000, "NIFTY 50", "Nifty 50"),
              "BANKNIFTY": ("nse_cm", 26009, "NIFTY BANK", "Nifty Bank"),
              "SENSEX": ("bse_cm", 1, "SENSEX", "SENSEX")},
        OWN_BOOK=True, TAG="nsed", TAG_SL="nses",
        STRIKE_GAP={"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100, "NSECRUDE": 50},
        GAP_CHOICES={"NIFTY": (50, 100, 200), "BANKNIFTY": (100, 200, 500), "SENSEX": (100, 200, 500),
                     "NSECRUDE": (50, 100, 200)},
        REFRESH_AT=dt.time(8, 45),                         # before the 09:00 commodity open
        REPLAY_BUILD_AT=dt.time(23, 58),                   # after crude's 23:30 / 23:55 close
        # desk open = commodity hours (crude 09:00 - 23:30, 23:55 in winter); indices still 09:15 - 15:30
        HOURS={"open": "09:00", "close": "23:30", "winter_close": "23:55"},
    ),
    "BTC": dict(
        PORT=47684, DATA=HERE / "data_btc", LOG_NAME="btc_terminal.log", AUDIT_NAME="btc_audit",
        INSTRUMENTS=("BTC",),
        LABELS={"BTC": "Bitcoin"},
        SEGMENT={"BTC": "delta"},
        SYMBOL={},
        # options settle on Delta's BTC index -> the chain's ATM follows the index (streamed as spot_price)
        SPOT={"BTC": ("delta", BTC_SPOT_TOKEN, ".DEXBTUSD", None)},
        OWN_BOOK=True, TAG="btcd", TAG_SL="btcs",
        STRIKE_GAP={"BTC": 200},
        GAP_CHOICES={"BTC": (200, 500, 1000, 2000)},
        RESET_AT=dt.time(17, 35),                          # after Delta's 17:30 IST daily expiry
        REFRESH_AT=dt.time(17, 35),                        # same moment: fresh contract list (new dailies)
        REPLAY_BUILD_AT=dt.time(17, 33),
        HOURS={"always": True, "open": "17:35", "close": "17:35"},
        BROKER="DELTA", BROKER_LABEL="Delta", CCY="$", LIVE_ORDERS=False,
        UNIT_LABEL="contract", LOT_NOTE="1 contract = 0.001 BTC",
        MAX_LOTS_PER_ORDER=5000, CONFIRM_LOTS=1000, MAX_QTY_LOTS_CHOICE=1000,
        DAY_START=dt.time(17, 35),
    ),
}
_COMMON = dict(BROKER="KOTAK", BROKER_LABEL="Kotak P", CCY="₹", LIVE_ORDERS=True, UNIT_LABEL="lot", LOT_NOTE="",
               RESET_AT=dt.time(0, 5), DAY_START=dt.time(0, 0),
               MAX_LOTS_PER_ORDER=300,     # hard cap: an order above this is refused (typo guard)
               CONFIRM_LOTS=50,            # the screen asks "are you sure" above this many lots
               MAX_QTY_LOTS_CHOICE=100)    # qty box offers 1..this many lots
globals().update({**_COMMON, **_DESKS[DESK]})
if os.getenv("TERMINAL_PORT"):             # preview / test copies only: never set on the real engines
    PORT = int(os.getenv("TERMINAL_PORT"))
DESK_LABEL = f"{DESK} Desk"
# NEWS button (news.py: public headlines, read-only) - MCX desk only (owner, 12-Nov-2026)
NEWS = os.getenv("TERMINAL_NEWS", "1" if DESK == "MCX" else "0") == "1"
# the other desk, for the switch at the top of the screen
DESK_URLS = {"MCX": os.getenv("MCX_DESK_URL", "https://mcx.trading.contact"),
             "NSE": os.getenv("NSE_DESK_URL", "https://nse.trading.contact"),
             "BTC": os.getenv("BTC_DESK_URL", "https://btc.trading.contact")}

_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def day(now: "dt.datetime | None" = None) -> str:
    """The desk's trading day as YYYY-MM-DD (names every day file: books, MTM, guards, OI, audit...).
    MCX / NSE: the IST calendar date. BTC: days run DAY_START -> DAY_START (17:35) and carry the date they
    END on - 14-Nov 18:00 IST is day 15-Nov, the day whose daily options expire 15-Nov 17:30."""
    now = now or dt.datetime.now(_IST)
    if DAY_START == dt.time(0, 0):
        return now.date().isoformat()
    start = dt.timedelta(hours=DAY_START.hour, minutes=DAY_START.minute)
    return (now + (dt.timedelta(days=1) - start)).date().isoformat()

HOST = "127.0.0.1"          # VPS-local only (reached through the Cloudflare tunnel / an SSH tunnel)
# PORT (above) is also the single-instance lock: one process can bind it

ENV_SUFFIX = "_D"           # Kotak P = env keys KOTAK_*_D

SUB_BUDGET = 2800           # Kotak allows 3000 subscribed tokens per connection; keep headroom
SUB_CHUNK = 400             # tokens per subscribe frame
STRIKE_CHOICES = (5, 7, 10, 12, 15, 20)
DEFAULT_STRIKES = 10

# ── orders (paper now, same checks will guard live) ──
MODE = "PAPER"              # start-up mode; LIVE only when the OWNER arms it on the screen (resets daily)
# MAX_LOTS_PER_ORDER / CONFIRM_LOTS / MAX_QTY_LOTS_CHOICE: per desk (_COMMON / _DESKS above)
PAPER_CARRY = False          # paper positions do NOT carry to the next day (daily reset at RESET_AT)
MKT_BUFFER_PCT = 1.0        # "market" = limit at best ask/bid +/- this % (Kotak takes no MARKET orders)

DATA.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
