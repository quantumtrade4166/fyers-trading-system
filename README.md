# Nifty F&O Data Pipeline
### 5-min OHLCV · ~180 stocks · Fyers API → Google Drive

---

## Folder Structure

```
fyers_data_pipeline/
├── config/
│   ├── settings.py          ← Edit your credentials here
│   ├── symbols.py           ← Nifty F&O stock universe
│   └── access_token.txt     ← Auto-generated daily token (gitignore this)
│   └── gdrive_credentials.json  ← Your Google service account key (gitignore this)
├── auth/
│   └── fyers_auth.py        ← Fyers OAuth login + token management
├── downloader/
│   └── fetch_ohlcv.py       ← Core data fetcher (chunked, incremental)
├── storage/
│   └── gdrive_sync.py       ← Google Drive uploader
├── tracker/
│   ├── manifest.py          ← Data manifest manager
│   └── data_manifest.json   ← Auto-generated: what data you have
├── data/                    ← Local Parquet staging area
│   └── NSE_RELIANCE_EQ/
│       ├── 2023/ohlcv_5min.parquet
│       └── 2024/ohlcv_5min.parquet
├── logs/
│   └── ingestion.log
├── run_pipeline.py          ← Main entry point
└── requirements.txt
```

---

## One-Time Setup

### Step 1 — Install dependencies
```cmd
pip install -r requirements.txt
```

### Step 2 — Configure Fyers credentials
Edit `config/settings.py`:
```python
FYERS_APP_ID     = "YOUR_APP_ID"      # e.g. "XC1234567-100"
FYERS_SECRET_KEY = "YOUR_SECRET_KEY"
```

### Step 3 — Set up Google Drive access
1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project → Enable **Google Drive API**
3. Go to **IAM & Admin → Service Accounts** → Create service account
4. Create a JSON key → Download it
5. Save as `config/gdrive_credentials.json`
6. Copy the service account email (looks like `xxx@yyy.iam.gserviceaccount.com`)
7. Share a Google Drive folder with that email (Editor access)

### Step 4 — First-time Fyers login
```cmd
python auth/fyers_auth.py
```
This opens your browser, you log in to Fyers, paste the redirect URL back.
Token is saved and reused automatically until it expires (daily).

---

## Usage

### Check what data you have
```cmd
python run_pipeline.py --mode status
```

### First-time full download (~2 years of history)
```cmd
python run_pipeline.py --mode full --sync
```
⚠️ This will take 3-5 hours for 180+ symbols. Run overnight.

### Daily incremental update (run after 3:30 PM every trading day)
```cmd
python run_pipeline.py --mode update --sync
```
Only downloads missing data since last run. Takes ~10-15 minutes.

### Download specific symbols only
```cmd
python run_pipeline.py --mode update --symbols NSE:RELIANCE-EQ NSE:INFY-EQ
```

### Sync to Drive without downloading
```cmd
python run_pipeline.py --mode sync
```

### Rebuild manifest if it gets out of sync
```cmd
python run_pipeline.py --mode rebuild
```

---

## Daily Automation (Windows Task Scheduler)

Set up a task to run daily at 4:00 PM:
1. Open **Task Scheduler** → Create Basic Task
2. Trigger: Daily at 4:00 PM (weekdays only)
3. Action: Start a program
   - Program: `C:\Python311\python.exe`
   - Arguments: `run_pipeline.py --mode update --sync`
   - Start in: `C:\path\to\fyers_data_pipeline`

---

## Data Format

Each Parquet file contains:
| Column   | Type     | Example                  |
|----------|----------|--------------------------|
| datetime | datetime | 2024-01-15 09:15:00      |
| symbol   | string   | NSE:RELIANCE-EQ          |
| open     | float64  | 2450.50                  |
| high     | float64  | 2462.00                  |
| low      | float64  | 2448.75                  |
| close    | float64  | 2458.30                  |
| volume   | int64    | 145230                   |

---

## Loading Data for Backtesting

```python
import pandas as pd
from pathlib import Path

# Load one symbol
df = pd.read_parquet("data/NSE_RELIANCE_EQ/2024/ohlcv_5min.parquet")

# Load all years for one symbol
dfs = [pd.read_parquet(f) for f in Path("data/NSE_RELIANCE_EQ").rglob("*.parquet")]
df = pd.concat(dfs).sort_values("datetime")

# Load multiple symbols
symbols = ["NSE_RELIANCE_EQ", "NSE_INFY_EQ", "NSE_HDFCBANK_EQ"]
all_data = {s: pd.concat([pd.read_parquet(f) for f in Path(f"data/{s}").rglob("*.parquet")])
            for s in symbols}
```

---

## Estimated Storage

| Scope | Size |
|-------|------|
| 180 symbols × 2 years × 5-min | ~2.5 GB |
| Nifty index only | ~15 MB |
| Per symbol per year | ~7 MB |
