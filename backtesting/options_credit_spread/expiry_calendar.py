"""
Empirically-derived Nifty weekly-expiry calendar.

The option chain data only ever contains the current front-week contract
(no expiry column). Total chain-wide OI at 09:15 builds up Mon->expiry-day
and craters on the first trading day of the next cycle (open interest resets
because the old contract is gone and the new one starts with ~0 built-up OI).
That crater is used to detect every rollover, and therefore every expiry
date, directly from the data -- this handles holiday-shifted expiries and
the Thursday->Tuesday expiry-day change automatically, with no hardcoded
weekday rules.

Usage:
    from backtesting.options_credit_spread.expiry_calendar import ExpiryCalendar
    cal = ExpiryCalendar()
    cal.expiry_on_or_after("2022-01-05")   -> "2022-01-06"
    cal.dte("2022-01-05")                   -> 1
"""
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "NSE_NIFTY_OPTIONS"
CALENDAR_PATH = DATA_DIR / "expiry_calendar.csv"
ROLLOVER_OI_RATIO_THRESHOLD = 0.7


def build_calendar(years=range(2021, 2027)):
    """Derive the expiry calendar from chain-wide 09:15 OI and cache it to CSV."""
    frames = []
    for year in years:
        path = DATA_DIR / str(year) / "ohlcv_1min.parquet"
        if not path.exists():
            continue
        df = pd.read_parquet(path, columns=["datetime", "date", "oi"])
        sub = df[df["datetime"].dt.strftime("%H:%M") == "09:15"]
        daily_oi = sub.groupby("date")["oi"].sum().reset_index()
        frames.append(daily_oi)

    all_oi = pd.concat(frames, ignore_index=True)
    all_oi["date"] = pd.to_datetime(all_oi["date"])
    all_oi = all_oi.sort_values("date").reset_index(drop=True)
    all_oi["oi_ratio"] = all_oi["oi"] / all_oi["oi"].shift(1)

    trading_days = all_oi["date"].tolist()
    rollover_mask = all_oi["oi_ratio"] < ROLLOVER_OI_RATIO_THRESHOLD
    rollover_days = all_oi.loc[rollover_mask, "date"].tolist()

    # expiry_date = the trading day immediately before each detected rollover
    expiry_dates = []
    for rd in rollover_days:
        idx = trading_days.index(rd)
        if idx == 0:
            continue
        expiry_dates.append(trading_days[idx - 1])

    # Recover missed weeks: a >10 calendar-day gap between two detected
    # expiries either means (a) the source data is genuinely missing for that
    # stretch (e.g. the ~Dec-Jan gap present every year from 2022 on), or
    # (b) a real expiry existed but its OI reset was too thin to cross the
    # threshold (seen in low-liquidity early-2021 weeks). Only case (b) has
    # trading days actually sitting inside the gap to search.
    expiry_dates_sorted = sorted(set(expiry_dates))
    all_recovered = []
    for _pass in range(5):  # iterate: a wide gap can hide more than one missed week
        recovered = []
        for prev_exp, next_exp in zip(expiry_dates_sorted, expiry_dates_sorted[1:]):
            if (next_exp - prev_exp).days <= 10:
                continue
            # skip the trading day right after prev_exp -- that's the already-known
            # rollover day (naturally low ratio), not a new one to search for
            prev_idx = trading_days.index(prev_exp)
            search_start = trading_days[prev_idx + 2] if prev_idx + 2 < len(trading_days) else next_exp
            between = all_oi[(all_oi["date"] >= search_start) & (all_oi["date"] < next_exp)]
            if len(between) < 4:
                continue  # genuinely no data in this window (e.g. Dec-Jan gap)
            # local minimum oi_ratio marks the rollover day; expiry = day before it
            rollover_idx = between["oi_ratio"].idxmin()
            rollover_date = all_oi.loc[rollover_idx, "date"]
            idx = trading_days.index(rollover_date)
            if idx > 0 and trading_days[idx - 1] not in expiry_dates_sorted:
                recovered.append(trading_days[idx - 1])
        if not recovered:
            break
        all_recovered.extend(recovered)
        expiry_dates_sorted = sorted(set(expiry_dates_sorted + recovered))

    if all_recovered:
        print(f"Recovered {len(all_recovered)} low-liquidity expiry weeks: {sorted(d.date() for d in all_recovered)}")

    # the very last cycle in the dataset has no rollover yet to mark its end;
    # skip it -- dte() simply won't resolve past the last known expiry.
    cal_df = pd.DataFrame({"expiry_date": expiry_dates_sorted})
    cal_df["expiry_date"] = cal_df["expiry_date"].dt.strftime("%Y-%m-%d")
    cal_df.to_csv(CALENDAR_PATH, index=False)
    print(f"Built calendar: {len(cal_df)} expiry dates -> {CALENDAR_PATH}")
    return cal_df


class ExpiryCalendar:
    def __init__(self):
        if not CALENDAR_PATH.exists():
            build_calendar()
        df = pd.read_csv(CALENDAR_PATH, parse_dates=["expiry_date"])
        self.expiry_dates = sorted(df["expiry_date"].tolist())

    def expiry_on_or_after(self, date):
        """Front-week expiry date active for the given trade date."""
        return self.nth_expiry(date, 0)

    def nth_expiry(self, date, n: int = 0):
        """The n-th expiry on/after `date`. n=0 front week, n=1 next week, etc.

        Needed to trade the 0-3 DTE signals: when the front week is too close to
        expiry to enter, the trade goes into the following expiry instead of
        being discarded.
        """
        date = pd.Timestamp(date).normalize()
        upcoming = [exp for exp in self.expiry_dates if exp >= date]
        if n < len(upcoming):
            return upcoming[n]
        return None

    def dte(self, date, n: int = 0):
        """Calendar days from `date` to the n-th expiry. None if unknown."""
        date = pd.Timestamp(date).normalize()
        exp = self.nth_expiry(date, n)
        if exp is None:
            return None
        return (exp - date).days

    # ---- trading-day DTE (weekends AND holidays excluded) ----

    def set_trading_days(self, trading_days):
        """Provide the real trading calendar so DTE can be counted in sessions.

        Calendar-day DTE silently distorted the entry rule: a Friday signal
        scored 6 (two of them a weekend) while a Monday signal with the SAME
        number of sessions left scored 3. With a >=4 gate that collapsed into
        "only trade on Fridays" — 93 of v1's 98 pre-2025 trades.
        """
        self._trading_days = sorted(str(d) for d in trading_days)

    def trading_dte(self, date, n: int = 0):
        """Sessions from `date` (exclusive) to the n-th expiry (inclusive).

        Weekly cycle => 4 (Friday) .. 0 (expiry day itself).
        """
        if not hasattr(self, "_trading_days"):
            raise RuntimeError("call set_trading_days() first")
        exp = self.nth_expiry(date, n)
        if exp is None:
            return None
        d0, d1 = str(pd.Timestamp(date).date()), str(pd.Timestamp(exp).date())
        return sum(1 for d in self._trading_days if d0 < d <= d1)

    def is_cycle_first_session(self, date):
        """Is `date` the first trading session of the current expiry cycle?

        That session is the one holding a full weekend before expiry, which is
        why it is the only day worth selling the near contract on. In the
        Thursday-expiry era it is Friday; after the Sep-2025 switch to Tuesday
        expiry it is Wednesday. Holiday-shortened weeks are handled too: the
        first session is still the first session, even if only 3 sessions
        remain instead of 4.
        """
        d = str(pd.Timestamp(date).date())
        prior = [e for e in self.expiry_dates if str(e.date()) < d]
        if not prior:
            return True
        last_exp = str(prior[-1].date())
        after = [x for x in self._trading_days if x > last_exp]
        return bool(after) and after[0] == d

    def cycle_entry_expiry(self, date):
        """Which expiry to trade, per the cycle rule.

        First session of the cycle -> the CURRENT week (near contract).
        Any later session         -> the NEXT week (premium is too thin near expiry).

        Returns (expiry, trading_dte, n).
        """
        n = 0 if self.is_cycle_first_session(date) else 1
        exp = self.nth_expiry(date, n)
        if exp is None:
            return None, None, None
        return exp, self.trading_dte(date, n), n

    def tradeable_expiry_trading(self, date, min_dte: int):
        """First expiry with at least `min_dte` SESSIONS left.

        Returns (expiry, trading_dte, n). n=0 front week, n=1 next week.
        """
        for n in range(4):
            exp = self.nth_expiry(date, n)
            if exp is None:
                break
            tdte = self.trading_dte(date, n)
            if tdte is not None and tdte >= min_dte:
                return exp, tdte, n
        return None, None, None

    def tradeable_expiry(self, date, min_dte: int):
        """First expiry at least `min_dte` days out, and its DTE.

        This is what replaces "skip the signal": instead of discarding a 0-3 DTE
        signal because the front week is too close, step out to the next expiry
        that satisfies the rule. Returns (expiry, dte, n) or (None, None, None).
        """
        for n in range(4):                       # front week .. 3 weeks out
            exp = self.nth_expiry(date, n)
            if exp is None:
                break
            dte = (exp - pd.Timestamp(date).normalize()).days
            if dte >= min_dte:
                return exp, dte, n
        return None, None, None


if __name__ == "__main__":
    # The OI-crater heuristic that build_calendar() implements was measured
    # against Breeze in Aug 2026 and found to produce ~11% false positives
    # (31 of 289 dates had no contract at all). The calendar on disk has since
    # been replaced with the Breeze-verified version, so rebuilding would be a
    # regression. Require an explicit --force.
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite the Breeze-validated calendar with the OI heuristic")
    args = ap.parse_args()

    if not args.force:
        print(f"Refusing to rebuild {CALENDAR_PATH}.")
        print("The calendar on disk is Breeze-validated; the OI heuristic that this")
        print("script implements produced 31 phantom expiry dates out of 289.")
        print("Re-validate instead:  python -m options.breeze.validate_calendar --all")
        print("Use --force only if you know you want the heuristic version.")
        raise SystemExit(1)

    cal_df = build_calendar()
    print(cal_df.head(10))
    print(cal_df.tail(10))
