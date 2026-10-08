"""
1-hour Supertrend on Nifty spot, built from the 1-min spot series embedded
in the options data, and the resulting flip events (the actual entry/exit
triggers for the credit-spread strategy).

Hour bars are aligned to market open (09:15, 10:15, ... 15:15) via a 15-min
resample offset, and labelled by their CLOSE time (label="right") so that a
flip detected on a bar is only actionable at the timestamp it's labelled
with -- no lookahead.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from backtesting.indicators import add_supertrend

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def build_hourly_supertrend(spot_1min: pd.Series, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """1-hour OHLC (from spot) + Supertrend direction, bars labelled by close time.

    Bins are anchored per trading day at 09:15 (not a plain calendar-hour grid),
    giving 6 full-hour bars + one final 15-min bar labelled 15:30 (the real
    market close) instead of a nonsensical 16:15 label."""
    df = spot_1min.to_frame("price")
    day_start = pd.Series(df.index.normalize() + pd.Timedelta(hours=9, minutes=15), index=df.index)
    minutes_since_open = (df.index.to_series() - day_start).dt.total_seconds() / 60.0
    bin_idx = (minutes_since_open // 60).astype(int).clip(upper=6)
    is_last_bin = bin_idx == 6
    label_time = day_start + (bin_idx + 1) * pd.Timedelta(hours=1)
    label_time = label_time.where(~is_last_bin, day_start + pd.Timedelta(minutes=375))
    df["bin_label"] = label_time

    hourly = df.groupby("bin_label")["price"].agg(["first", "max", "min", "last"])
    hourly.columns = ["open", "high", "low", "close"]
    hourly = add_supertrend(hourly, period=period, multiplier=multiplier)
    return hourly


def extract_flip_events(hourly_df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> list:
    """List of (confirmed_timestamp, new_direction) for every direction change.
    new_direction: 1 = uptrend just started (green), -1 = downtrend just started (red).
    Warmup rows (direction == 0) are ignored."""
    dir_col = f"supertrend_dir_{period}_{multiplier}"
    d = hourly_df[dir_col]
    d = d[d != 0]
    changed = d[d != d.shift(1)]
    # first valid direction isn't a "flip" from anything -- still usable as the
    # first entry opportunity, so keep it; drop only if it's literally the
    # first row of the whole series with no prior context
    events = list(zip(changed.index.tolist(), changed.values.tolist()))
    return events


REAL_HOURLY_PATH = Path(__file__).resolve().parents[2] / "data" / "NSE_NIFTY50_INDEX" / "ohlcv_60min.parquet"


def load_real_hourly_supertrend(period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend on TRUE 1-hour NIFTY index candles from Fyers (TradingView-equivalent).

    build_hourly_supertrend() takes each hour's high/low from minute closes, which
    compresses ATR and adds whipsaw flips. Against the manual trade sheet these
    candles match 82% of entry dates exactly vs 68%. Bars are already labelled by
    close time (see fetch_nifty_hourly_fyers.py).
    """
    if not REAL_HOURLY_PATH.exists():
        raise FileNotFoundError(
            f"{REAL_HOURLY_PATH} missing - run "
            "python -m backtesting.options_credit_spread.fetch_nifty_hourly_fyers")
    hourly = pd.read_parquet(REAL_HOURLY_PATH).set_index("datetime")[["open", "high", "low", "close"]]
    return add_supertrend(hourly, period=period, multiplier=multiplier)
