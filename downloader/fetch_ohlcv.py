# ============================================================
# downloader/fetch_ohlcv.py
# Downloads 5-min OHLCV from Fyers API in date chunks.
# Handles rate limits, retries, and incremental updates.
# ============================================================

import time
import logging
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.settings import (
    RESOLUTION, MAX_DAYS_PER_CALL, HISTORY_DAYS,
    SLEEP_BETWEEN_CALLS, DATA_DIR, PARQUET_COMPRESSION
)

logger = logging.getLogger(__name__)


# ── Date chunk helpers ───────────────────────────────────────

def get_date_chunks(start_date: date, end_date: date, chunk_days: int = 90):
    """
    Split a date range into chunks (Fyers limit: 100 days per call).
    We use 90 days to stay safely under the limit.
    """
    chunks = []
    current = start_date
    while current < end_date:
        chunk_end = min(current + timedelta(days=chunk_days), end_date)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


# ── Parquet path helpers ─────────────────────────────────────

def get_parquet_path(symbol: str, year: int) -> Path:
    """
    Returns the Parquet file path for a symbol and year.
    Structure: data/{clean_symbol}/{year}/ohlcv_5min.parquet
    """
    clean = symbol.replace(":", "_").replace("-", "_")
    path = DATA_DIR / clean / str(year)
    path.mkdir(parents=True, exist_ok=True)
    return path / "ohlcv_5min.parquet"


def load_existing(symbol: str) -> pd.DataFrame | None:
    """Load all existing Parquet files for a symbol across years."""
    clean = symbol.replace(":", "_").replace("-", "_")
    symbol_dir = DATA_DIR / clean
    if not symbol_dir.exists():
        return None

    dfs = []
    for parquet_file in symbol_dir.rglob("ohlcv_5min.parquet"):
        try:
            dfs.append(pd.read_parquet(parquet_file))
        except Exception as e:
            logger.warning(f"Could not read {parquet_file}: {e}")

    if not dfs:
        return None

    df = pd.concat(dfs).drop_duplicates(subset=["datetime"]).sort_values("datetime")
    return df


def get_last_downloaded_date(symbol: str) -> date | None:
    """Check the most recent data point we have for a symbol."""
    df = load_existing(symbol)
    if df is None or df.empty:
        return None
    return pd.to_datetime(df["datetime"].max()).date()


# ── Core API fetch ───────────────────────────────────────────

def fetch_chunk(fyers_client, symbol: str, from_date: date, to_date: date,
                max_retries: int = 3) -> pd.DataFrame | None:
    """
    Fetch one chunk of OHLCV data from Fyers API.
    Returns a cleaned DataFrame or None on failure.
    """
    data = {
        "symbol": symbol,
        "resolution": RESOLUTION,
        "date_format": "1",
        "range_from": str(from_date),
        "range_to": str(to_date),
        "cont_flag": "1",
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = fyers_client.history(data=data)

            if response.get("s") != "ok":
                msg = response.get("message", "Unknown error")
                logger.warning(f"{symbol} chunk {from_date} to {to_date} attempt {attempt}: {msg}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            candles = response.get("candles", [])
            if not candles:
                logger.info(f"{symbol}: No data for {from_date} to {to_date}")
                return None

            df = pd.DataFrame(
                candles,
                columns=["epoch", "open", "high", "low", "close", "volume"]
            )
            df["datetime"] = (
                pd.to_datetime(df["epoch"], unit="s")
                .dt.tz_localize("UTC")
                .dt.tz_convert("Asia/Kolkata")
                .dt.tz_localize(None)
            )
            df["symbol"] = symbol
            df = df.drop(columns=["epoch"])
            df = df[["datetime", "symbol", "open", "high", "low", "close", "volume"]]

            # Filter to market hours only
            df = df[
                (df["datetime"].dt.time >= pd.Timestamp("09:15").time()) &
                (df["datetime"].dt.time <= pd.Timestamp("15:30").time())
            ]

            logger.info(f"{symbol}: Got {len(df)} bars for {from_date} to {to_date}")
            return df

        except Exception as e:
            logger.error(f"{symbol} attempt {attempt} exception: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    logger.error(f"{symbol}: All {max_retries} attempts failed for {from_date} to {to_date}")
    return None


# ── Save to Parquet (year-partitioned) ───────────────────────

def save_to_parquet(df: pd.DataFrame, symbol: str):
    """
    Save DataFrame to year-partitioned Parquet files.
    Merges with existing data to avoid duplicates.
    """
    for year, year_df in df.groupby(df["datetime"].dt.year):
        path = get_parquet_path(symbol, year)

        if path.exists():
            existing = pd.read_parquet(path)
            combined = (
                pd.concat([existing, year_df])
                .drop_duplicates(subset=["datetime"])
                .sort_values("datetime")
            )
        else:
            combined = year_df.sort_values("datetime")

        combined.to_parquet(path, compression=PARQUET_COMPRESSION, index=False)
        logger.debug(f"Saved {len(combined)} rows to {path}")


# ── Main download function for one symbol ───────────────────

def download_symbol(fyers_client, symbol: str,
                    force_full: bool = False) -> dict:
    """
    Download all available history for one symbol.
    If data already exists, only fetches the missing (incremental update).

    Returns a status dict: {symbol, bars_downloaded, date_range, status}
    """
    end_date   = date.today() - timedelta(days=1)
    start_date = date.today() - timedelta(days=HISTORY_DAYS)

    # Incremental: start from day after last downloaded date
    if not force_full:
        last = get_last_downloaded_date(symbol)
        if last:
            if last >= end_date:
                logger.info(f"{symbol}: Already up to date (last={last})")
                return {"symbol": symbol, "bars": 0, "status": "up_to_date"}
            start_date = last + timedelta(days=1)
            logger.info(f"{symbol}: Incremental from {start_date} (last={last})")
        else:
            logger.info(f"{symbol}: Full download from {start_date}")

    chunks = get_date_chunks(start_date, end_date, chunk_days=90)
    all_frames = []

    for chunk_start, chunk_end in chunks:
        df = fetch_chunk(fyers_client, symbol, chunk_start, chunk_end)
        if df is not None and not df.empty:
            all_frames.append(df)
        time.sleep(SLEEP_BETWEEN_CALLS)

    if not all_frames:
        return {"symbol": symbol, "bars": 0, "status": "no_data"}

    full_df = (
        pd.concat(all_frames)
        .drop_duplicates(subset=["datetime"])
        .sort_values("datetime")
    )
    save_to_parquet(full_df, symbol)

    return {
        "symbol":    symbol,
        "bars":      len(full_df),
        "date_from": str(full_df["datetime"].min().date()),
        "date_to":   str(full_df["datetime"].max().date()),
        "status":    "success",
    }