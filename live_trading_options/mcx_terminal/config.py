"""
mcx_terminal/config.py — settings for the MCX order terminal (Kotak P).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]                 # ...\fyers_data_pipeline
HERE = Path(__file__).resolve().parent
DATA = HERE / "data"                                       # scrip cache, books (Stage 2+)
STATIC = HERE / "static"
LOG_DIR = ROOT / "logs"

HOST = "127.0.0.1"          # Stage 1: VPS-local only (reach it through an SSH tunnel / later the tunnel)
PORT = 47680                # also the single-instance lock: one process can bind it

ENV_SUFFIX = "_D"           # Kotak P = env keys KOTAK_*_D
INSTRUMENTS = ("CRUDEOIL", "GOLDM", "SILVERM")
LABELS = {"CRUDEOIL": "Crude Oil", "GOLDM": "Gold Mini", "SILVERM": "Silver Mini"}

SUB_BUDGET = 2800           # Kotak allows 3000 subscribed tokens per connection; keep headroom
SUB_CHUNK = 400             # tokens per subscribe frame
STRIKE_CHOICES = (5, 7, 10, 12, 15, 20)
DEFAULT_STRIKES = 10

# ── orders (paper now, same checks will guard live) ──
MODE = "PAPER"              # LIVE only ever by the owner, in a later stage
MAX_LOTS_PER_ORDER = 300    # hard cap: an order above this is refused (typo guard)
CONFIRM_LOTS = 50           # the screen asks "are you sure" above this many lots
MKT_BUFFER_PCT = 1.0        # "market" = limit at best ask/bid +/- this % (Kotak takes no MARKET orders)
MAX_QTY_LOTS_CHOICE = 100   # qty box offers 1..this many lots

DATA.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
