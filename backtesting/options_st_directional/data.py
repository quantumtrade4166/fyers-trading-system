"""Real-data provider for the engine: Breeze NIFTY index + option 1-minute bars.

Reuses
  * options.breeze.datasource.BreezeData        -- cached, fetch-on-demand option bars
  * data/BREEZE_OPTIONS/NIFTY/index_1min.parquet -- NIFTY index (fetch_index_spot)
  * options_credit_spread.expiry_calendar        -- Breeze-verified expiry dates,
                                                    trading-session DTE
  * backtesting.resample / backtesting.indicators -- 5-min bars, Supertrend

Candle convention: 5-min candles labelled at their open (09:15 = 09:15-09:19:59
one-minute bars), built per session from 1-minute bars before 15:30. The lone
15:30 bar Breeze returns is dropped.

Supertrend: indicators.add_supertrend == TradingView ta.supertrend (hl2 bands,
Wilder/RMA ATR, close compared with the current final band). The first
`period - 1` candles of a series are blanked, so no value exists before ATR has
`period` true ranges behind it.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

from backtesting.options_st_directional.candles import (  # noqa: F401
    to_5min, supertrend, clean_index_closing_auction)
from backtesting.options_credit_spread.expiry_calendar import ExpiryCalendar
from options.breeze.config import DATA_DIR

INDEX_PATH = DATA_DIR / "NIFTY" / "index_1min.parquet"

# Exchange holidays AFTER the last index bar on disk. Needed only to count
# sessions to an expiry that lies beyond the data. 2026-10-20 is confirmed by the
# 19-Oct (Monday) expiry Breeze returns for that week.
FUTURE_HOLIDAYS = {"2026-10-20"}


class BreezeProvider:
    def __init__(self, cfg, verbose: bool = True):
        from options.breeze.datasource import BreezeData
        self.cfg = cfg
        self.verbose = verbose
        self.bd = BreezeData(interval="1minute", stock_code="NIFTY", verbose=verbose)

        spot1 = pd.read_parquet(INDEX_PATH)
        spot1["datetime"] = pd.to_datetime(spot1["datetime"])
        self.spot1 = clean_index_closing_auction(spot1.sort_values("datetime"))
        days = sorted(self.spot1["datetime"].dt.strftime("%Y-%m-%d").unique())
        future = pd.bdate_range(pd.Timestamp(days[-1]) + pd.Timedelta(days=1),
                                periods=40).strftime("%Y-%m-%d")
        self.trading_days = days + [d for d in future if d not in FUTURE_HOLIDAYS]
        self.last_data_day = days[-1]

        self.cal = ExpiryCalendar()
        self.cal.set_trading_days(self.trading_days)

        # continuous spot Supertrend over the whole index history (warm-up included)
        self.spot5 = supertrend(to_5min(self.spot1, cfg.timeframe_min),
                                cfg.st_period, cfg.st_mult)
        self._one_min: dict = {}
        self._opt5: dict = {}
        self.missing: list = []        # (date, contract, what)

    # ---------- calendar ----------
    def days_in_range(self, start: str, end: str) -> list:
        return [d for d in self.trading_days if start <= d <= end and d <= self.last_data_day]

    def prev_day(self, day: str, k: int = 1):
        i = self.trading_days.index(day)
        return self.trading_days[i - k] if i - k >= 0 else None

    # ---------- spot ----------
    def spot_candles(self, day: str) -> pd.DataFrame:
        return self.spot5[self.spot5.index.strftime("%Y-%m-%d") == day]

    # ---------- instrument selection ----------
    def select_contract(self, day: str, ts, spot: float) -> dict:
        step = self.cfg.strike_step
        strike = int(round(spot / step) * step)
        front = self.cal.nth_expiry(day, 0)
        left = self.cal.trading_dte(day, 0)
        if left is not None and left <= self.cfg.switch_sessions_left:
            n = 1
            reason = (f"front {front.date()} has {left} session(s) left "
                      f"(<= {self.cfg.switch_sessions_left}) -> next week")
        else:
            n = 0
            reason = f"front week, {left} session(s) left"
        expiry = self.cal.nth_expiry(day, n)
        if expiry is None:
            raise RuntimeError(f"No expiry in calendar for {day} (n={n})")
        return {"strike": strike, "expiry": expiry, "reason": reason}

    # ---------- option bars ----------
    def _bars(self, day: str, contract) -> pd.DataFrame:
        key = (day, contract)
        if key not in self._one_min:
            expiry, strike, opt = contract
            df = self.bd.bars(expiry=expiry, strike=strike,
                              right="call" if opt == "CE" else "put", trade_date=day)
            if not df.empty:
                df = df.drop_duplicates("datetime").sort_values("datetime")
                df = df.dropna(subset=["open", "high", "low", "close"])
            self._one_min[key] = df
        return self._one_min[key]

    def option_candles(self, day: str, contract) -> pd.DataFrame:
        key = (day, contract)
        if key in self._opt5:
            return self._opt5[key]
        today = self._bars(day, contract)
        if today.empty:
            self.missing.append((day, contract, "no bars for the day"))
            self._opt5[key] = pd.DataFrame()
            return self._opt5[key]
        frames = []
        for k in range(self.cfg.option_warmup_days, 0, -1):
            p = self.prev_day(day, k)
            if p is not None:
                frames.append(self._bars(p, contract))
        frames.append(today)
        c5 = supertrend(to_5min(pd.concat(frames), self.cfg.timeframe_min),
                        self.cfg.st_period, self.cfg.st_mult)
        c5 = c5[c5.index.strftime("%Y-%m-%d") == day]
        # candles with no traded minute are absent, never forward-filled
        grid = pd.date_range(f"{day} 09:15", f"{day} 15:25",
                             freq=f"{self.cfg.timeframe_min}min")
        gaps = grid.difference(c5.index)
        if len(gaps):
            self.missing.append((day, contract, f"{len(gaps)} empty 5-min candles"))
        self._opt5[key] = c5
        return c5

    def minute_bars(self, day: str, contract, start, end) -> pd.DataFrame:
        """1-minute bars of a contract with start <= minute < end."""
        df = self._bars(day, contract)
        if df.empty:
            return df
        return df[(df["datetime"] >= pd.Timestamp(start)) & (df["datetime"] < pd.Timestamp(end))]

    # ---------- execution ----------
    def fill(self, day: str, contract, ts):
        """Open of the first 1-minute bar at/after ts, within fill_search_min."""
        df = self._bars(day, contract)
        if df.empty:
            return None
        ts = pd.Timestamp(ts)
        win = df[(df["datetime"] >= ts)
                 & (df["datetime"] < ts + pd.Timedelta(minutes=self.cfg.fill_search_min))]
        if win.empty:
            return None
        r = win.iloc[0]
        return pd.Timestamp(r["datetime"]), float(r["open"])

    def last_price(self, day: str, contract, ts):
        df = self._bars(day, contract)
        before = df[df["datetime"] < pd.Timestamp(ts)] if not df.empty else df
        if before.empty:
            return None
        r = before.iloc[-1]
        return pd.Timestamp(r["datetime"]), float(r["close"])
