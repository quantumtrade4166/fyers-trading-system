"""
Adapter that lets the intraday pivot backtest read Breeze-format option data.

The engine was written against data/NSE_NIFTY_OPTIONS (Fyers), whose schema
differs from Breeze:

  Fyers  : datetime, strike_label, option_type, OHLC, volume, oi, iv,
           strike_price, spot, date       -- front week only, spot embedded
  Breeze : datetime, expiry, strike_price, option_type, OHLC, volume, oi,
           date, stock_code               -- carries expiry, NO spot column

Two things are reconciled here:

1. **Spot.** Breeze option files have no spot. Put-call parity was measured
   against known spot and runs ~33 points rich (it recovers the FORWARD, not
   spot), so the real index is used instead, fetched by
   options.breeze.fetch_index_spot into NIFTY/index_1min.parquet.

2. **Expiry.** Breeze can hold several expiries per date. The Fyers dataset was
   front-week only and the strategy is intraday, so only the nearest expiry per
   date is kept -- otherwise the same (datetime, strike, type) key appears more
   than once and the pivot collapses them arbitrarily.

The public surface matches OptionsDataLoader, so the strategy code is unchanged.
"""
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BREEZE_DIR = Path(__file__).resolve().parents[2] / "data" / "BREEZE_OPTIONS"


class BreezeOptionsLoader:
    """Drop-in replacement for OptionsDataLoader, backed by Breeze parquets."""

    def __init__(self, stock_code: str = "NIFTY", years=None):
        self.stock_code = stock_code
        self._day_chain_cache: dict = {}
        self._day_pivot_cache: dict = {}
        self._spot_cache = None

        root = BREEZE_DIR / stock_code
        frames = []
        for year_dir in sorted(root.glob("[0-9][0-9][0-9][0-9]")):
            if years and int(year_dir.name) not in years:
                continue
            path = year_dir / "ohlcv_1minute.parquet"
            if path.exists():
                frames.append(pd.read_parquet(path))
        if not frames:
            raise RuntimeError(f"No Breeze option parquets under {root}")

        df = pd.concat(frames, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["expiry"] = pd.to_datetime(df["expiry"])
        df["date"] = df["date"].astype(str)
        df["option_type"] = df["option_type"].str.upper().str[:4].replace({"CALL": "CALL", "PUT": "PUT"})
        df["strike_price"] = df["strike_price"].astype(int)

        # keep only the nearest expiry per trading date (front week)
        nearest = df.groupby("date")["expiry"].transform("min")
        df = df[df["expiry"] == nearest].copy()

        self._df = df
        self._by_day = {d: g for d, g in df.groupby("date", sort=False)}

    # ── same interface as OptionsDataLoader ──────────────────────────────

    def get_day_chain(self, date_str: str) -> pd.DataFrame:
        if date_str not in self._day_chain_cache:
            self._day_chain_cache[date_str] = self._by_day.get(
                date_str, pd.DataFrame(columns=["datetime", "strike_price", "option_type", "close"])
            )
        return self._day_chain_cache[date_str]

    def get_day_pivot(self, date_str: str, field: str = "close") -> pd.DataFrame:
        key = (date_str, field)
        if key not in self._day_pivot_cache:
            chain = self.get_day_chain(date_str)
            if chain.empty:
                self._day_pivot_cache[key] = pd.DataFrame()
            else:
                self._day_pivot_cache[key] = chain.pivot_table(
                    index="datetime", columns=["strike_price", "option_type"],
                    values=field, aggfunc="last",
                )
        return self._day_pivot_cache[key]

    def has_strike(self, date_str: str, strike_price: int, option_type: str) -> bool:
        return (strike_price, option_type) in self.get_day_pivot(date_str).columns

    def leg_series(self, date_str: str, strike_price: int, option_type: str,
                   field: str = "close") -> pd.Series:
        pivot = self.get_day_pivot(date_str, field)
        col = (strike_price, option_type)
        if col not in pivot.columns:
            return pd.Series(dtype="float64")
        return pivot[col].dropna()

    def spot_1min_series(self) -> pd.Series:
        """Real index 1-minute closes (not parity-derived)."""
        if self._spot_cache is None:
            path = BREEZE_DIR / self.stock_code / "index_1min.parquet"
            if not path.exists():
                raise RuntimeError(
                    f"Index spot not found: {path}\n"
                    "Fetch it:  python -m options.breeze.fetch_index_spot --start ... --end ..."
                )
            idx = pd.read_parquet(path)
            s = idx.set_index("datetime")["close"].sort_index()
            self._spot_cache = s[~s.index.duplicated(keep="last")]
        return self._spot_cache

    def spot_series_day(self, date_str: str) -> pd.Series:
        s = self.spot_1min_series()
        return s[s.index.strftime("%Y-%m-%d") == date_str]

    def available_dates(self) -> list:
        return sorted(self._by_day.keys())
