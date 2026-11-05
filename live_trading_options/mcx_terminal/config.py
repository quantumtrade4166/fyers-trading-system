"""
mcx_terminal/config.py — settings for the order terminal (Kotak P).

ONE code base, TWO desks. The desk is picked when the engine starts, by the environment variable
TERMINAL_DESK (default MCX):
  MCX  MCX commodity options (CRUDEOIL, GOLD, GOLDM, SILVERM)     port 47680, data\\
  NSE  index options (NIFTY, BANKNIFTY on NSE, SENSEX on BSE)    port 47682, data_nse\\
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
if DESK not in ("MCX", "NSE"):
    raise SystemExit(f"TERMINAL_DESK must be MCX or NSE, not {DESK!r}")

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
        INSTRUMENTS=("NIFTY", "BANKNIFTY", "SENSEX"),
        LABELS={"NIFTY": "Nifty", "BANKNIFTY": "Bank Nifty", "SENSEX": "Sensex"},
        SEGMENT={"NIFTY": "nse_fo", "BANKNIFTY": "nse_fo", "SENSEX": "bse_fo"},
        SYMBOL={},
        # index spot: (segment, feed token, label, REST quote name) - checked on Kotak 04-Nov-2026: the feed
        # takes the token; REST quotes() refuses the token but answers to the name (backup price source)
        SPOT={"NIFTY": ("nse_cm", 26000, "NIFTY 50", "Nifty 50"),
              "BANKNIFTY": ("nse_cm", 26009, "NIFTY BANK", "Nifty Bank"),
              "SENSEX": ("bse_cm", 1, "SENSEX", "SENSEX")},
        OWN_BOOK=True, TAG="nsed", TAG_SL="nses",
        STRIKE_GAP={"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100},
        GAP_CHOICES={"NIFTY": (50, 100, 200), "BANKNIFTY": (100, 200, 500), "SENSEX": (100, 200, 500)},
        REFRESH_AT=dt.time(8, 45),                         # before the 09:15 open
        REPLAY_BUILD_AT=dt.time(15, 45),                   # after the 15:30 close
        HOURS={"open": "09:15", "close": "15:30"},
    ),
}
globals().update(_DESKS[DESK])
if os.getenv("TERMINAL_PORT"):             # preview / test copies only: never set on the real engines
    PORT = int(os.getenv("TERMINAL_PORT"))
DESK_LABEL = f"{DESK} Desk"
# the other desk, for the switch at the top of the screen
DESK_URLS = {"MCX": os.getenv("MCX_DESK_URL", "https://mcx.trading.contact"),
             "NSE": os.getenv("NSE_DESK_URL", "https://nse.trading.contact")}

HOST = "127.0.0.1"          # VPS-local only (reached through the Cloudflare tunnel / an SSH tunnel)
# PORT (above) is also the single-instance lock: one process can bind it

ENV_SUFFIX = "_D"           # Kotak P = env keys KOTAK_*_D

SUB_BUDGET = 2800           # Kotak allows 3000 subscribed tokens per connection; keep headroom
SUB_CHUNK = 400             # tokens per subscribe frame
STRIKE_CHOICES = (5, 7, 10, 12, 15, 20)
DEFAULT_STRIKES = 10

# ── orders (paper now, same checks will guard live) ──
MODE = "PAPER"              # start-up mode; LIVE only when the OWNER arms it on the screen (resets daily)
MAX_LOTS_PER_ORDER = 300    # hard cap: an order above this is refused (typo guard)
CONFIRM_LOTS = 50           # the screen asks "are you sure" above this many lots
PAPER_CARRY = False          # paper positions do NOT carry to the next day (daily reset at 00:05)
MKT_BUFFER_PCT = 1.0        # "market" = limit at best ask/bid +/- this % (Kotak takes no MARKET orders)
MAX_QTY_LOTS_CHOICE = 100   # qty box offers 1..this many lots

DATA.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
