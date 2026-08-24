"""Expiry-aware price access for the credit-spread backtest.

The original loader assumes every row is the front-week contract — true of
data/NSE_NIFTY_OPTIONS/, which is why 72% of the strategy's signals (those
firing at 0-3 DTE, which must be traded in the FOLLOWING expiry) had to be
discarded.

This layer adds the expiry dimension:

  * front-week contracts  -> served from the existing local parquet (free, fast)
  * any other expiry      -> fetched on demand from Breeze and cached to disk

Pricing is lazy and per-contract rather than per-chain. Scanning a whole chain
from Breeze would cost ~40 requests per entry; the strategy only ever needs the
short strike and the handful of strikes walking out to the long leg, so we fetch
exactly those.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd

from backtesting.options_credit_spread.data_loader import OptionsDataLoader
from backtesting.options_credit_spread.expiry_calendar import ExpiryCalendar


class ExpiryAwareLoader:
    def __init__(self, interval: str = "1minute", dry_run: bool = False,
                 verbose: bool = False):
        self.local = OptionsDataLoader()
        self.cal = ExpiryCalendar()
        self.interval = interval
        self.dry_run = dry_run
        self.verbose = verbose

        from options.breeze.datasource import BreezeData
        self.breeze = BreezeData(interval=interval, stock_code="NIFTY",
                                 dry_run=dry_run, verbose=verbose)

        self._series_cache: dict = {}
        self.local_hits = 0
        self.breeze_reads = 0

    # ---------- routing ----------

    def is_front_week(self, date_str: str, expiry) -> bool:
        front = self.cal.expiry_on_or_after(date_str)
        return front is not None and pd.Timestamp(expiry).normalize() == front

    def leg_series(self, date_str: str, expiry, strike: int,
                   option_type: str) -> pd.Series:
        """Per-minute close series for one contract on one day. Empty if absent."""
        key = (date_str, str(pd.Timestamp(expiry).date()), int(strike), option_type)
        if key in self._series_cache:
            return self._series_cache[key]

        if self.is_front_week(date_str, expiry):
            s = self.local.leg_series(date_str, strike, option_type)
            if not s.empty:
                self.local_hits += 1
                self._series_cache[key] = s
                return s
            # Local front-week data can still be missing this strike (outside the
            # dataset's ATM+/-10 window) — fall through to Breeze rather than
            # forcing the strategy down its off-grid approximation path.

        right = "call" if option_type.upper().startswith("C") else "put"
        try:
            df = self.breeze.bars(expiry=expiry, strike=strike, right=right,
                                  trade_date=date_str)
        except Exception as exc:
            if self.verbose:
                print(f"      [breeze miss] {date_str} {strike}{option_type[0]} "
                      f"exp {pd.Timestamp(expiry).date()}: {exc}")
            df = pd.DataFrame()

        self.breeze_reads += 1
        if df.empty and self.dry_run:
            # Dry run fetches nothing, so without a stand-in the strategy would
            # bail at the first missing price and stop exploring — massively
            # understating the true fetch count. Synthesise a plausible price so
            # it walks the same path it will on the real run.
            s = self._synthetic_series(date_str, strike, option_type)
        elif df.empty:
            s = pd.Series(dtype="float64")
        else:
            s = df.set_index("datetime")["close"].dropna().sort_index()
        self._series_cache[key] = s
        return s

    def _synthetic_series(self, date_str: str, strike: int,
                          option_type: str) -> pd.Series:
        """Rough option price for dry-run costing only. Never used for P&L."""
        spot_day = self.spot_series_day(date_str)
        if spot_day.empty:
            return pd.Series(dtype="float64")
        if option_type.upper().startswith("P"):
            intrinsic = (strike - spot_day).clip(lower=0)
        else:
            intrinsic = (spot_day - strike).clip(lower=0)
        # Time value peaks at the money and decays with distance — enough for the
        # "long leg under 50% of the short premium" walk to terminate realistically.
        moneyness = ((spot_day - strike) / 300.0) ** 2
        time_value = 100.0 * (-moneyness).apply(lambda x: 2.718281828 ** x)
        return (intrinsic + time_value).astype("float64")

    def price_at(self, date_str: str, expiry, strike: int, option_type: str,
                 ts) -> float | None:
        """Close at (or immediately before) `ts`. None if the contract has no data."""
        s = self.leg_series(date_str, expiry, strike, option_type)
        if s.empty:
            return None
        upto = s[s.index <= ts]
        if upto.empty:
            return None
        return float(upto.iloc[-1])

    def has_strike(self, date_str: str, expiry, strike: int,
                   option_type: str) -> bool:
        return not self.leg_series(date_str, expiry, strike, option_type).empty

    # ---------- pass-throughs (expiry-independent) ----------

    def spot_series_day(self, date_str: str) -> pd.Series:
        return self.local.spot_series_day(date_str)

    def spot_1min_series(self) -> pd.Series:
        return self.local.spot_1min_series()

    def available_dates(self) -> list:
        return self.local.available_dates()

    def spot_at(self, date_str: str, ts) -> float | None:
        s = self.spot_series_day(date_str)
        if s.empty:
            return None
        upto = s[s.index <= ts]
        return float(upto.iloc[-1]) if not upto.empty else None

    # ---------- reporting ----------

    def cost_report(self) -> None:
        print(f"\n  Local (front-week) series reads : {self.local_hits:,}")
        print(f"  Breeze series reads             : {self.breeze_reads:,}")
        self.breeze.cost_report()
