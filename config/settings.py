# ============================================================
# config/settings.py
# Central configuration — edit this file to match your setup
# ============================================================

import os
from pathlib import Path

# ── Fyers API Credentials ────────────────────────────────────
FYERS_APP_ID     = "W09OMXQB8J-100"
FYERS_SECRET_KEY = "F3SKSM9JSG"
FYERS_REDIRECT_URI = "https://127.0.0.1"   # must match your Fyers app settings

# Token file — stored locally, refreshed daily
TOKEN_FILE = Path("config/access_token.txt")

# ── Data Settings ────────────────────────────────────────────
RESOLUTION    = "5"          # 5-minute bars (Fyers uses "5" for 5min)
MAX_DAYS_PER_CALL = 100      # Fyers hard limit for intraday history per call
MARKET_START  = "09:15"
MARKET_END    = "15:30"

# How far back to download (in days)
HISTORY_DAYS  = 730          # ~2 years

# ── Local Paths ──────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent.parent
DATA_DIR      = BASE_DIR / "data"          # local staging before upload
LOG_DIR       = BASE_DIR / "logs"
TRACKER_DIR   = BASE_DIR / "tracker"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
TRACKER_DIR.mkdir(exist_ok=True)

# ── Google Drive Settings ────────────────────────────────────
# Folder name that will be created inside your Google Drive root
GDRIVE_FOLDER_NAME = "NiftyFNO_MarketData"

# Path to your Google service account JSON key file
# Download from: Google Cloud Console → Service Accounts → Keys
GDRIVE_CREDENTIALS_FILE = Path("config/gdrive_credentials.json")

# ── Parquet Settings ─────────────────────────────────────────
# Partition structure: data/{symbol}/{year}/data.parquet
# This keeps files small and makes partial reads fast
PARQUET_COMPRESSION = "snappy"     # fast read/write, good compression

# ── Logging ──────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FILE  = LOG_DIR / "ingestion.log"

# ── Rate Limiting ────────────────────────────────────────────
# Fyers allows ~10 req/sec; stay conservative
SLEEP_BETWEEN_CALLS = 0.5    # seconds between API calls
SLEEP_BETWEEN_SYMBOLS = 1.0  # seconds between symbols