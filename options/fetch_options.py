import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import time
import logging
import pandas as pd
from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from config.settings import DATA_DIR, SLEEP_BETWEEN_CALLS, PARQUET_COMPRESSION

logger = logging.getLogger(__name__)

OPTIONS_DIR  = DATA_DIR / "options" / "NIFTY"
OPT_RESOLUTION = "1"           # 1-minute bars


def get_contract_path(expiry: date, strike: int, option_type: str) -> Path:
    """
    data/options/NIFTY/{expiry_date}/{strike}_{type}.parquet
    e.g. data/options/NIFTY/2025-05-22/24950_CE.parquet
    """
    folder = OPTIONS_DIR / str(expiry)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{strike}_{option_type}.parquet"


def fetch_contract(fyers_client, symbol: str, from_date: date, to_date: date,
                   max_retries: int = 3) -> pd.DataFrame | None:
    """
    Fetch 1-min OHLCV (+ OI if available) for one option contract.
    Single call is enough — each contract spans ≤30 days, under Fyers 100-day limit.
    """
    data = {
        "symbol":      symbol,
        "resolution":  OPT_RESOLUTION,
        "date_format": "1",
        "range_from":  str(from_date),
        "range_to":    str(to_date),
        "cont_flag":   "1",
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = fyers_client.history(data=data)

            if response.get("s") != "ok":
                msg = response.get("message", "Unknown error")
                logger.warning(f"{symbol} attempt {attempt}: {msg}")
                if attempt < max_retries:
                    time.sleep(2 ** attempt)
                continue

            candles = response.get("candles", [])
            if not candles:
                return None

            # Fyers returns 6 columns (equities) or 7 (derivatives, incl. OI)
            if len(candles[0]) == 7:
                cols = ["epoch", "open", "high", "low", "close", "volume", "oi"]
            else:
                cols = ["epoch", "open", "high", "low", "close", "volume"]

            df = pd.DataFrame(candles, columns=cols)
            df["datetime"] = (
                pd.to_datetime(df["epoch"], unit="s")
                .dt.tz_localize("UTC")
                .dt.tz_convert("Asia/Kolkata")
                .dt.tz_localize(None)
            )
            df["symbol"] = symbol
            df = df.drop(columns=["epoch"])

            ordered = ["datetime", "symbol", "open", "high", "low", "close", "volume"]
            if "oi" in df.columns:
                ordered.append("oi")
            df = df[ordered]

            df = df[
                (df["datetime"].dt.time >= pd.Timestamp("09:15").time()) &
                (df["datetime"].dt.time <= pd.Timestamp("15:30").time())
            ]

            logger.info(f"{symbol}: {len(df)} bars ({from_date} → {to_date})")
            return df

        except Exception as e:
            logger.error(f"{symbol} attempt {attempt}: {e}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    return None


def save_contract(df: pd.DataFrame, expiry: date, strike: int, option_type: str):
    """Write contract data to parquet, merging with any pre-existing rows."""
    path = get_contract_path(expiry, strike, option_type)

    if path.exists():
        existing = pd.read_parquet(path)
        df = (
            pd.concat([existing, df])
            .drop_duplicates(subset=["datetime"])
            .sort_values("datetime")
        )
    else:
        df = df.sort_values("datetime")

    df.to_parquet(path, compression=PARQUET_COMPRESSION, index=False)


def download_contract(fyers_client, symbol: str, expiry: date,
                      strike: int, option_type: str, from_date: date) -> dict:
    """
    Fetch and persist one option contract.
    Skips download if parquet already exists on disk.
    """
    path = get_contract_path(expiry, strike, option_type)
    if path.exists():
        return {"symbol": symbol, "bars": 0, "status": "up_to_date"}

    df = fetch_contract(fyers_client, symbol, from_date, expiry)
    if df is None or df.empty:
        return {"symbol": symbol, "bars": 0, "status": "no_data"}

    save_contract(df, expiry, strike, option_type)
    return {
        "symbol":    symbol,
        "bars":      len(df),
        "date_from": str(df["datetime"].min().date()),
        "date_to":   str(df["datetime"].max().date()),
        "status":    "success",
    }
