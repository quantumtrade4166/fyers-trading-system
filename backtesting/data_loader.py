# ============================================================
# backtesting/data_loader.py
#
# Fast Parquet data loader for Nifty F&O intraday backtesting.
#
# Data layout on disk:
#   G:/fyers_data_pipeline/data/{SYMBOL_FOLDER}/{YEAR}/ohlcv_5min.parquet
#
#   SYMBOL_FOLDER = Fyers symbol with special chars → underscores
#   e.g. NSE:RELIANCE-EQ  →  NSE_RELIANCE_EQ
#        NSE:M&M-EQ       →  NSE_M_M_EQ
#
# Parquet columns: datetime, symbol, open, high, low, close, volume
#
# Usage:
#   loader = DataLoader()
#   df = loader.load("NSE:RELIANCE-EQ")
#   df = loader.load("NSE:RELIANCE-EQ", start="2025-01-01", end="2025-06-30")
#   data = loader.load_many(["NSE:RELIANCE-EQ", "NSE:INFY-EQ"])
#   syms = loader.available_symbols()
# ============================================================

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Union

import pandas as pd

logger = logging.getLogger(__name__)

# ── Default paths (relative to this file's parent = project root) ─────────────
_PROJECT_ROOT = Path(__file__).parent.parent   # G:/fyers_data_pipeline
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"
_MANIFEST_FILE    = _PROJECT_ROOT / "tracker" / "data_manifest.json"

# Market session times (IST)
_MARKET_OPEN  = "09:15"
_MARKET_CLOSE = "15:30"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _symbol_to_folder(symbol: str) -> str:
    """
    Convert a Fyers symbol string to its on-disk folder name.

    Rules: colon, hyphen, ampersand → underscore.
      NSE:RELIANCE-EQ  →  NSE_RELIANCE_EQ
      NSE:BAJAJ-AUTO-EQ → NSE_BAJAJ_AUTO_EQ
      NSE:M&M-EQ        → NSE_M_M_EQ
    """
    return symbol.replace(":", "_").replace("-", "_").replace("&", "_")


def _load_manifest() -> dict:
    """Load tracker manifest; return empty dict if not found."""
    if _MANIFEST_FILE.exists():
        try:
            return json.loads(_MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Could not read manifest: {exc}")
    return {}


def _to_timestamp(value: Optional[Union[str, date, datetime]]) -> Optional[pd.Timestamp]:
    """Convert str / date / datetime / None to pd.Timestamp."""
    return pd.Timestamp(value) if value is not None else None


# ── Main class ────────────────────────────────────────────────────────────────

class DataLoader:
    """
    Fast loader for year-partitioned Parquet OHLCV data.

    All returned DataFrames have:
      - Index  : datetime (pd.DatetimeIndex, ascending, IST)
      - Columns: open, high, low, close (float64) | volume (int64) | symbol (str)

    Parameters
    ----------
    data_dir : Path or str, optional
        Root of the Parquet data tree.
        Default: G:/fyers_data_pipeline/data/
    market_hours_only : bool, default True
        Strip rows outside 09:15–15:30 (handles any stray pre/post-market bars).
    cache : bool, default True
        Keep loaded DataFrames in memory. Speeds up repeated calls with the
        same symbol/date-range during a single backtest session.

    Examples
    --------
    >>> loader = DataLoader()

    # Full 2-year history for one stock
    >>> df = loader.load("NSE:RELIANCE-EQ")

    # One calendar year
    >>> df = loader.load("NSE:INFY-EQ", start="2025-01-01", end="2025-12-31")

    # Multiple stocks — returns dict keyed by symbol
    >>> data = loader.load_many(["NSE:RELIANCE-EQ", "NSE:TCS-EQ"])
    >>> reliance_df = data["NSE:RELIANCE-EQ"]

    # What symbols are available?
    >>> syms = loader.available_symbols()
    >>> print(len(syms), "symbols ready")

    # Metadata from manifest
    >>> info = loader.symbol_info("NSE:RELIANCE-EQ")
    >>> print(info["date_from"], "→", info["date_to"])
    """

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        market_hours_only: bool = True,
        cache: bool = True,
    ):
        self.data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR
        self.market_hours_only = market_hours_only
        self._cache: Optional[dict[str, pd.DataFrame]] = {} if cache else None

        if not self.data_dir.exists():
            raise FileNotFoundError(
                f"Data directory not found: {self.data_dir}\n"
                f"Run: python run_pipeline.py --mode status"
            )

    # ── Core load methods ─────────────────────────────────────────────────────

    def load(
        self,
        symbol: str,
        start: Optional[Union[str, date, datetime]] = None,
        end:   Optional[Union[str, date, datetime]] = None,
    ) -> pd.DataFrame:
        """
        Load OHLCV data for one symbol.

        Parameters
        ----------
        symbol : str
            Fyers format — e.g. "NSE:RELIANCE-EQ"
        start : str | date | datetime, optional
            Earliest date to include (inclusive). None = all available data.
            e.g. "2025-01-01" or date(2025, 1, 1)
        end : str | date | datetime, optional
            Latest date to include (inclusive). None = all available data.
            e.g. "2025-12-31"

        Returns
        -------
        pd.DataFrame
            datetime-indexed OHLCV, sorted ascending.

        Raises
        ------
        FileNotFoundError
            If the symbol has no data on disk.
        """
        cache_key = f"{symbol}|{start}|{end}"
        if self._cache is not None and cache_key in self._cache:
            logger.debug(f"Cache hit: {cache_key}")
            return self._cache[cache_key]

        df = self._read_parquets(symbol, start, end)
        logger.info(
            f"Loaded {symbol}: {len(df):,} bars  "
            f"({df.index[0].date()} → {df.index[-1].date()})"
        )

        if self._cache is not None:
            self._cache[cache_key] = df

        return df

    def load_many(
        self,
        symbols: list[str],
        start: Optional[Union[str, date, datetime]] = None,
        end:   Optional[Union[str, date, datetime]] = None,
    ) -> dict[str, pd.DataFrame]:
        """
        Load OHLCV data for multiple symbols.

        Parameters
        ----------
        symbols : list[str]
            List of Fyers symbols, e.g. ["NSE:RELIANCE-EQ", "NSE:INFY-EQ"]
        start, end : optional
            Same as load() — applied to all symbols.

        Returns
        -------
        dict[str, pd.DataFrame]
            Keyed by Fyers symbol. Failed/missing symbols are skipped
            (warning logged) so one bad symbol doesn't break the whole batch.
        """
        result = {}
        failed = []

        for sym in symbols:
            try:
                result[sym] = self.load(sym, start, end)
            except FileNotFoundError as exc:
                logger.warning(str(exc))
                failed.append(sym)
            except Exception as exc:
                logger.error(f"Unexpected error loading {sym}: {exc}")
                failed.append(sym)

        if failed:
            logger.warning(
                f"Skipped {len(failed)} symbols with no/corrupt data: {failed}"
            )

        logger.info(
            f"load_many complete: {len(result)}/{len(symbols)} symbols loaded."
        )
        return result

    # ── Discovery & metadata ──────────────────────────────────────────────────

    def available_symbols(self) -> list[str]:
        """
        Return a sorted list of all symbols that have data on disk.

        Uses the tracker manifest (fast, ~1ms).
        Falls back to a disk scan (slow, reads one row per symbol) if the
        manifest is missing.

        Returns
        -------
        list[str]
            Fyers-format symbols, e.g. ["NSE:AARTIIND-EQ", "NSE:ABB-EQ", ...]
        """
        manifest = _load_manifest()
        symbols = manifest.get("symbols", {})
        if symbols:
            result = sorted(
                sym for sym, meta in symbols.items()
                if meta.get("status") in ("success", "up_to_date")
            )
            if result:
                return result
            # Manifest present but all statuses are stale/no_data — fall through
            logger.warning("Manifest stale (all no_data); scanning disk (slow).")
        else:
            logger.warning("Manifest not found; scanning disk (slow).")
        return sorted(self._scan_symbols_from_disk())

    def symbol_info(self, symbol: str) -> dict:
        """
        Return manifest metadata for a symbol.

        Returns
        -------
        dict with keys: status, bars_total, date_from, date_to, last_checked
        Returns {} if symbol not in manifest.
        """
        return _load_manifest().get("symbols", {}).get(symbol, {})

    def date_range(self, symbol: str) -> tuple[Optional[str], Optional[str]]:
        """
        Return (date_from, date_to) strings for a symbol, from the manifest.

        Returns (None, None) if symbol not in manifest.
        """
        info = self.symbol_info(symbol)
        return info.get("date_from"), info.get("date_to")

    def summary(self) -> pd.DataFrame:
        """
        Return a DataFrame summarising all available symbols.

        Columns: symbol, status, date_from, date_to, bars_total, last_checked
        Sorted by symbol name.
        """
        manifest = _load_manifest()
        symbols = manifest.get("symbols", {})
        rows = [
            {
                "symbol": sym,
                "status": meta.get("status"),
                "date_from": meta.get("date_from"),
                "date_to": meta.get("date_to"),
                "bars_total": meta.get("bars_total", 0),
                "last_checked": meta.get("last_checked"),
            }
            for sym, meta in symbols.items()
        ]
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)

    def clear_cache(self):
        """Clear the in-memory DataFrame cache."""
        if self._cache is not None:
            self._cache.clear()
            logger.debug("DataLoader cache cleared.")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _read_parquets(
        self,
        symbol: str,
        start: Optional[Union[str, date, datetime]],
        end:   Optional[Union[str, date, datetime]],
    ) -> pd.DataFrame:
        """
        Core read logic:
        1. Find the symbol folder on disk.
        2. Filter to only the year sub-folders that overlap the date range.
        3. Read + concatenate Parquet files.
        4. Filter to exact date range and optionally market hours.
        5. Set datetime as index, sort ascending.
        """
        folder = self.data_dir / _symbol_to_folder(symbol)
        if not folder.exists():
            raise FileNotFoundError(
                f"No data folder for symbol {symbol!r}.\n"
                f"Expected path : {folder}\n"
                f"To download   : python run_pipeline.py --mode update --symbols {symbol}"
            )

        start_ts = _to_timestamp(start)
        end_ts   = _to_timestamp(end)
        if end_ts is not None:
            # Make end inclusive for the full day
            end_ts = end_ts.replace(hour=23, minute=59, second=59)

        # ── Select relevant year folders ──────────────────────────────────────
        year_dirs = [
            d for d in sorted(folder.iterdir())
            if d.is_dir() and d.name.isdigit()
        ]
        if start_ts:
            year_dirs = [y for y in year_dirs if int(y.name) >= start_ts.year]
        if end_ts:
            year_dirs = [y for y in year_dirs if int(y.name) <= end_ts.year]

        if not year_dirs:
            raise FileNotFoundError(
                f"No data for {symbol!r} in the requested date range "
                f"({start} → {end})."
            )

        # ── Read Parquet files ────────────────────────────────────────────────
        parts = []
        for year_dir in year_dirs:
            pq_file = year_dir / "ohlcv_5min.parquet"
            if pq_file.exists():
                parts.append(pd.read_parquet(pq_file))
            else:
                logger.warning(f"Missing Parquet file: {pq_file}")

        if not parts:
            raise FileNotFoundError(
                f"Parquet files exist in folders for {symbol!r} but none could be read."
            )

        df = pd.concat(parts, ignore_index=True)

        # ── Set datetime index ────────────────────────────────────────────────
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime").sort_index()

        # ── Exact date range filter ───────────────────────────────────────────
        if start_ts:
            df = df[df.index >= start_ts]
        if end_ts:
            df = df[df.index <= end_ts]

        # ── Market hours filter (09:15 – 15:30) ──────────────────────────────
        if self.market_hours_only:
            t_open  = pd.Timestamp(_MARKET_OPEN).time()
            t_close = pd.Timestamp(_MARKET_CLOSE).time()
            times   = df.index.time
            df = df[(times >= t_open) & (times <= t_close)]

        if df.empty:
            logger.warning(f"No bars returned for {symbol!r} with given filters.")

        return df

    def _scan_symbols_from_disk(self) -> list[str]:
        """
        Slow fallback: scan data directory and read one row per symbol
        to recover the Fyers symbol string stored in the 'symbol' column.
        """
        symbols = []
        for sym_folder in sorted(self.data_dir.iterdir()):
            if not sym_folder.is_dir():
                continue
            pq_files = sorted(sym_folder.rglob("ohlcv_5min.parquet"))
            if pq_files:
                try:
                    df = pd.read_parquet(pq_files[0], columns=["symbol"])
                    if not df.empty:
                        symbols.append(df["symbol"].iloc[0])
                except Exception as exc:
                    logger.debug(f"Could not read {pq_files[0]}: {exc}")
        return symbols
