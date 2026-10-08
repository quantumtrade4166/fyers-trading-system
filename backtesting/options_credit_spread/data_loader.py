"""
Fast access layer over data/NSE_NIFTY_OPTIONS/{year}/ohlcv_1min.parquet.

Two access patterns the strategy needs:
  - a day's full chain (long format) to scan strikes for entry selection
  - a day's chain pivoted wide (datetime x (strike_price, option_type) -> close)
    for fast per-minute lookups while a position is held

Both are cached per day so repeated access (a multi-day hold revisiting the
same day, or many trades entered on the same day) doesn't re-scan the year's
parquet each time.
"""
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "NSE_NIFTY_OPTIONS"


class OptionsDataLoader:
    def __init__(self):
        self._year_frames = {}      # year -> full year DataFrame
        self._day_index = {}        # year -> {date_str: sub-DataFrame} (groupby cache)
        self._day_chain_cache = {}  # date_str -> long-format day df
        self._day_pivot_cache = {}  # date_str -> wide pivot df
        self._spot_1min_cache = None

    # ── internal ─────────────────────────────────────────────────────────

    def _load_year(self, year: int) -> pd.DataFrame:
        if year not in self._year_frames:
            path = DATA_DIR / str(year) / "ohlcv_1min.parquet"
            self._year_frames[year] = pd.read_parquet(path)
        return self._year_frames[year]

    def _year_day_index(self, year: int) -> dict:
        if year not in self._day_index:
            df = self._load_year(year)
            self._day_index[year] = {d: g for d, g in df.groupby("date", sort=False)}
        return self._day_index[year]

    # ── public ───────────────────────────────────────────────────────────

    def get_day_chain(self, date_str: str) -> pd.DataFrame:
        """Long-format chain for one trading day: datetime, strike_label,
        option_type, OHLCV, oi, iv, strike_price, spot. Empty df if the day
        isn't in the data (holiday or the known Dec-Jan gap)."""
        if date_str not in self._day_chain_cache:
            year = int(date_str[:4])
            day_index = self._year_day_index(year)
            self._day_chain_cache[date_str] = day_index.get(
                date_str, pd.DataFrame(columns=["datetime", "strike_price", "option_type", "close", "spot"])
            )
        return self._day_chain_cache[date_str]

    def get_day_pivot(self, date_str: str, field: str = "close") -> pd.DataFrame:
        """Wide pivot for one day: index=datetime, columns=(strike_price, option_type), values=`field`."""
        key = (date_str, field)
        if key not in self._day_pivot_cache:
            chain = self.get_day_chain(date_str)
            if chain.empty:
                self._day_pivot_cache[key] = pd.DataFrame()
            else:
                pivot = chain.pivot_table(
                    index="datetime", columns=["strike_price", "option_type"], values=field, aggfunc="last"
                )
                self._day_pivot_cache[key] = pivot
        return self._day_pivot_cache[key]

    def has_strike(self, date_str: str, strike_price: int, option_type: str) -> bool:
        pivot = self.get_day_pivot(date_str)
        return (strike_price, option_type) in pivot.columns

    def leg_series(self, date_str: str, strike_price: int, option_type: str, field: str = "close") -> pd.Series:
        """Per-minute price series for one strike/type on one day. Empty if not present that day."""
        pivot = self.get_day_pivot(date_str, field)
        col = (strike_price, option_type)
        if col not in pivot.columns:
            return pd.Series(dtype="float64")
        return pivot[col].dropna()

    def spot_series_day(self, date_str: str) -> pd.Series:
        """Per-minute spot for one day (datetime -> spot). Empty if day missing."""
        chain = self.get_day_chain(date_str)
        if chain.empty:
            return pd.Series(dtype="float64")
        return chain.groupby("datetime")["spot"].first()

    def spot_1min_series(self) -> pd.Series:
        """Continuous per-minute Nifty spot series across the full dataset (all years, deduped)."""
        if self._spot_1min_cache is None:
            frames = []
            for year_dir in sorted(DATA_DIR.iterdir()):
                if not year_dir.is_dir():
                    continue
                path = year_dir / "ohlcv_1min.parquet"
                if not path.exists():
                    continue
                df = pd.read_parquet(path, columns=["datetime", "spot"])
                s = df.groupby("datetime")["spot"].first()
                frames.append(s)
            full = pd.concat(frames).sort_index()
            self._spot_1min_cache = full
        return self._spot_1min_cache

    def available_dates(self) -> list:
        """Sorted list of all trading dates present in the dataset (as strings)."""
        dates = set()
        for year_dir in sorted(DATA_DIR.iterdir()):
            if not year_dir.is_dir():
                continue
            path = year_dir / "ohlcv_1min.parquet"
            if not path.exists():
                continue
            df = pd.read_parquet(path, columns=["date"])
            dates.update(df["date"].unique().tolist())
        return sorted(dates)
