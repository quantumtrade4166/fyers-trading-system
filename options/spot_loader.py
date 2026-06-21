import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd
from datetime import date, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

# NIFTY 50 index symbol on Fyers
NIFTY_INDEX_SYMBOL = "NSE:NIFTY50-INDEX"


def get_nifty_daily_closes(fyers_client, from_date: date, to_date: date) -> pd.DataFrame:
    """
    Fetch NIFTY 50 daily OHLCV from Fyers.
    Returns DataFrame with columns: [date, open, high, low, close, volume].
    """
    data = {
        "symbol":     NIFTY_INDEX_SYMBOL,
        "resolution": "D",
        "date_format": "1",
        "range_from": str(from_date),
        "range_to":   str(to_date),
        "cont_flag":  "1",
    }

    response = fyers_client.history(data=data)
    if response.get("s") != "ok":
        raise RuntimeError(
            f"Failed to fetch NIFTY spot: {response.get('message', response)}"
        )

    candles = response.get("candles", [])
    if not candles:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
    df["date"] = (
        pd.to_datetime(df["epoch"], unit="s")
        .dt.tz_localize("UTC")
        .dt.tz_convert("Asia/Kolkata")
        .dt.date
    )
    return df[["date", "open", "high", "low", "close", "volume"]].sort_values("date").reset_index(drop=True)


def get_atm_for_expiry(closes: pd.DataFrame, expiry: date,
                        interval: int = 50) -> int:
    """
    Return the ATM strike for an expiry, based on the NIFTY close from ~7 days prior.
    Falls back to the earliest available close if no data before the reference date.
    """
    ref_date = expiry - timedelta(days=7)
    candidates = closes[closes["date"] <= ref_date]
    if candidates.empty:
        candidates = closes      # fallback: use earliest available bar
    spot = float(candidates.iloc[-1]["close"])
    return round(spot / interval) * interval
