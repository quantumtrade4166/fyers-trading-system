"""Fetch-on-demand options data with a local cache.

The alternative to bulk downloading. A bulk run pulls every strike in the window
(84 contracts/day) whether or not the strategy reads them; a backtest typically
touches a handful. So instead of downloading first and backtesting later, the
backtest asks for a contract-day and this fetches it only if it isn't cached.

    from options.breeze.datasource import BreezeData

    data = BreezeData(interval="1second")
    df = data.bars("NIFTY", expiry="2023-01-05", strike=18000,
                   right="call", trade_date="2023-01-04")

First read of a contract-day costs API calls; every later read is a local parquet
hit. Cache is shared with the bulk downloader — same files, same manifest — so
anything already downloaded is reused, and anything fetched here counts as
downloaded if you later run the bulk job.

Use `dry_run=True` to answer "what would this backtest cost?" without spending
a single call.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
from datetime import date

import pandas as pd

from options.breeze.config import DATA_DIR
from options.breeze.downloader import (
    Contract,
    Downloader,
    Manifest,
    STRIKE_STEP,
)


class BreezeData:
    """Cached, lazy access to Breeze options bars."""

    def __init__(self, interval: str = "1second", stock_code: str = "NIFTY",
                 exchange_code: str | None = None, dry_run: bool = False,
                 adaptive: bool = True, verbose: bool = True):
        self.interval = interval
        self.stock_code = stock_code
        self.exchange_code = exchange_code or ("BFO" if stock_code == "BSESEN" else "NFO")
        self.dry_run = dry_run
        self.verbose = verbose

        args = argparse.Namespace(
            interval=interval, stock_code=stock_code,
            exchange_code=self.exchange_code, adaptive=adaptive,
            workers=1, per_minute=None, ignore_daily_cap=True,
            flush_every=50_000, dry_run=dry_run,
            start="2000-01-01", end="2100-01-01", expiries=1, strikes=0,
        )
        # Reuse the downloader's fetch/normalise/flush machinery rather than
        # duplicating the windowing and cap handling.
        self._dl = Downloader.__new__(Downloader)
        self._dl.args = args
        self._dl.interval = interval
        self._dl.stock_code = stock_code
        self._dl.exchange_code = self.exchange_code
        self._dl.strike_step = STRIKE_STEP.get(stock_code, 50)
        self._dl.manifest = Manifest()
        self._dl.buffer = []
        self._dl.pending = []
        self._dl.rows_written = 0
        self._dl.contracts_done = 0
        self._dl.consecutive_empty = 0
        self._dl.hit_wall_at = None
        import time as _t
        self._dl.started = _t.time()

        # Authentication is deferred until a fetch is actually needed. A backtest
        # whose contract-days are all cached must not require a daily browser
        # login just to read local parquet.
        self._dl.client = None
        self._dl.throttle = None
        if not dry_run:
            from options.breeze.throttle import Throttle
            self._dl.throttle = Throttle(verbose=False, ignore_daily_cap=True)
        # Baseline so cost_report shows THIS session's spend, not today's running
        # total (the throttle counter persists across processes by design).
        self._calls_at_start = self._dl.throttle.used if self._dl.throttle else 0

        self._frames: dict = {}          # year -> cached DataFrame
        self.hits = 0
        self.misses = 0
        self.wanted: list = []           # contract-days requested (for dry runs)

    def _ensure_client(self) -> None:
        """Authenticate on first real fetch. Raises the usual expired-token error
        only if data genuinely has to be downloaded."""
        if self._dl.client is None:
            from options.breeze.session import get_client
            self._dl.client = get_client()

    # ---------- cache ----------

    def _parquet_path(self, year: int):
        return DATA_DIR / self.stock_code / str(year) / f"ohlcv_{self.interval}.parquet"

    def _year_frame(self, year: int) -> pd.DataFrame:
        if year not in self._frames:
            path = self._parquet_path(year)
            if path.exists():
                df = pd.read_parquet(path)
                df["expiry"] = pd.to_datetime(df["expiry"])
                self._frames[year] = df
            else:
                self._frames[year] = pd.DataFrame()
        return self._frames[year]

    def _invalidate(self, year: int) -> None:
        self._frames.pop(year, None)

    # ---------- public ----------

    def bars(self, stock_code: str | None = None, *, expiry, strike: int,
             right: str, trade_date) -> pd.DataFrame:
        """Bars for one contract on one day. Fetches only on a cache miss."""
        stock_code = stock_code or self.stock_code
        trade_date = str(pd.Timestamp(trade_date).date())
        expiry = str(pd.Timestamp(expiry).date())
        right = "call" if str(right).lower().startswith("c") else "put"
        strike = int(strike)

        contract = Contract(trade_date=trade_date, expiry_date=expiry,
                            strike_price=strike, right=right,
                            stock_code=stock_code, exchange_code=self.exchange_code)
        key = contract.key(self.interval)

        if self._dl.manifest.done(key):
            self.hits += 1
            return self._from_cache(contract)

        self.misses += 1
        self.wanted.append(contract)
        if self.dry_run:
            return pd.DataFrame()

        self._ensure_client()
        status, raw, error = self._dl.fetch_contract(contract)
        if status == "error":
            raise RuntimeError(f"Breeze fetch failed for {key}: {error}")

        if raw:
            self._dl.buffer.extend(self._dl.normalise(raw, contract))
        self._dl.pending.append((key, status, len(raw), error))
        self._dl.flush()                       # parquet first, then manifest
        self._invalidate(pd.Timestamp(trade_date).year)

        if self.verbose:
            print(f"    [fetch] {trade_date} {strike}{right[0].upper()} "
                  f"exp {expiry}: {len(raw):,} bars")
        return self._from_cache(contract)

    def _from_cache(self, contract: Contract) -> pd.DataFrame:
        year = pd.Timestamp(contract.trade_date).year
        df = self._year_frame(year)
        if df.empty:
            return df
        option_type = "CALL" if contract.right == "call" else "PUT"
        mask = (
            (df["date"].astype(str) == contract.trade_date)
            & (df["strike_price"] == contract.strike_price)
            & (df["option_type"].astype(str) == option_type)
            & (df["expiry"] == pd.Timestamp(contract.expiry_date))
        )
        return df[mask].sort_values("datetime").reset_index(drop=True)

    def cost_report(self) -> None:
        """What this session used — the number that matters for planning."""
        total = self.hits + self.misses
        print("\n" + "=" * 64)
        print(f"  Contract-days requested : {total:,}")
        print(f"    already cached        : {self.hits:,}")
        print(f"    fetched from API      : {self.misses:,}")
        if self._dl.throttle is not None:
            spent = self._dl.throttle.used - self._calls_at_start
            print(f"  API calls this session  : {spent:,}")
            print(f"  API calls today (total) : {self._dl.throttle.used:,}")
        if self.dry_run and self.misses:
            per_cd = 23 if self.interval == "1second" else 1
            print(f"  DRY RUN — would need ~{self.misses * per_cd:,} calls "
                  f"({self.misses:,} contract-days x ~{per_cd})")
        print("=" * 64)
