import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
XAUUSD 1-Second Historical Data Downloader
============================================
Pulls Dukascopy 1-second (s1) bid OHLC bars for Gold/USD, month by month,
from 1999-06 to present. Converts each month straight to Parquet and writes
it to Google Drive (H:) so the download survives local disk limits.

Google Drive must be running in "Stream" mode and the Windows session must
stay logged in (locked is fine) for H:\\ to remain mounted.

Run: .venv\\Scripts\\python.exe forex/download_xauusd_1s.py
"""

import subprocess
import time
import logging
import calendar
import pandas as pd
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config.settings import PARQUET_COMPRESSION
import forex.xauusd_manifest as mfst

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/xauusd_download.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

FOREX_DIR    = Path(__file__).parent
STAGING_DIR  = FOREX_DIR / "_staging"
OUTPUT_DIR   = Path("H:/My Drive/XAUUSD_1s_Data")

INSTRUMENT   = "xauusd"
TIMEFRAME    = "s1"
PRICE_TYPE   = "bid"

START_YEAR, START_MONTH = 2003, 5   # Dukascopy's true earliest xauusd data is 2003-05-05
SLEEP_BETWEEN_MONTHS = 3   # seconds, be polite to Dukascopy's servers
MAX_RETRIES = 3


def month_range(start_year: int, start_month: int):
    today = date.today()
    y, m = start_year, start_month
    while (y, m) <= (today.year, today.month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def output_path(year: int, month: int) -> Path:
    folder = OUTPUT_DIR / f"{year:04d}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"ohlcv_1s_{month:02d}.parquet"


def download_month_csv(year: int, month: int) -> Path | None:
    """Shell out to dukascopy-node CLI for one calendar month. Returns CSV path or None."""
    # Dukascopy's -to date is EXCLUSIVE, so use the 1st of the *next* month
    # to make sure the last day of this month is actually included.
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    date_from = f"{year:04d}-{month:02d}-01"
    date_to   = f"{next_year:04d}-{next_month:02d}-01"

    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    file_name = f"{year:04d}-{month:02d}"

    cmd = [
        "npx", "dukascopy-node",
        "-i", INSTRUMENT,
        "-from", date_from,
        "-to", date_to,
        "-t", TIMEFRAME,
        "-p", PRICE_TYPE,
        "-f", "csv",
        "-dir", str(STAGING_DIR),
        "-fn", file_name,
        "-r", "3",       # retries for a failed artifact
        "-fr",           # don't hard-fail the whole run after retries exhausted
    ]

    result = subprocess.run(
        cmd, cwd=str(FOREX_DIR), shell=True,
        capture_output=True, text=True, timeout=600,
    )

    csv_path = STAGING_DIR / f"{file_name}.csv"
    if result.returncode != 0 or not csv_path.exists():
        logger.warning(f"{year}-{month:02d}: download failed\n{result.stdout[-500:]}\n{result.stderr[-500:]}")
        return None

    return csv_path


def convert_and_save(csv_path: Path, year: int, month: int) -> int:
    """Convert raw CSV to Parquet on Google Drive, delete the CSV. Returns row count."""
    if csv_path.stat().st_size == 0:
        csv_path.unlink(missing_ok=True)
        return 0

    df = pd.read_csv(csv_path)
    if df.empty:
        csv_path.unlink(missing_ok=True)
        return 0

    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df = df.drop(columns=["timestamp"])
    df = df[["datetime", "open", "high", "low", "close"]].sort_values("datetime")

    # Safety net: keep only rows that actually belong to the target month
    df = df[(df["datetime"].dt.year == year) & (df["datetime"].dt.month == month)]

    df.to_parquet(output_path(year, month), compression=PARQUET_COMPRESSION, index=False)
    csv_path.unlink(missing_ok=True)
    return len(df)


def download_month(year: int, month: int) -> dict:
    try:
        for attempt in range(1, MAX_RETRIES + 1):
            csv_path = download_month_csv(year, month)
            if csv_path is not None:
                break
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
        else:
            return {"status": "failed", "rows": 0}

        rows = convert_and_save(csv_path, year, month)
        if rows == 0:
            return {"status": "no_data", "rows": 0}

        logger.info(f"{year}-{month:02d}: {rows:,} rows -> {output_path(year, month)}")
        return {"status": "success", "rows": rows}

    except Exception as e:
        logger.error(f"{year}-{month:02d}: unexpected error: {e}")
        return {"status": "failed", "rows": 0}


def main():
    if not OUTPUT_DIR.parent.exists():
        logger.error(f"Google Drive path not found: {OUTPUT_DIR.parent} — is Drive mounted at H:?")
        sys.exit(1)

    manifest = mfst.load_manifest()
    months = list(month_range(START_YEAR, START_MONTH))
    logger.info(f"XAUUSD 1s backfill: {len(months)} months, {START_YEAR}-{START_MONTH:02d} -> present")

    for year, month in months:
        if mfst.is_fetched(manifest, year, month):
            continue

        result = download_month(year, month)
        mfst.mark_fetched(manifest, year, month, result)
        mfst.save_manifest(manifest)

        time.sleep(SLEEP_BETWEEN_MONTHS)

    mfst.print_summary(manifest)


if __name__ == "__main__":
    main()
