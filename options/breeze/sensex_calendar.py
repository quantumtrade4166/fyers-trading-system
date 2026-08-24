"""Build a SENSEX spot series + expiry calendar from Breeze.

NIFTY has both locally (spot in the existing parquet, expiries in a calendar we
just validated). SENSEX has neither, and the downloader needs both: spot to
centre the strike window, expiries to know which contracts exist.

Spot is cheap — 1day candles return ~1000 rows per call, so years arrive in a
couple of calls. Expiries have to be discovered: BSE has moved the SENSEX weekly
expiry more than once (Friday -> Tuesday -> Thursday), so for each week we try
the plausible weekdays until one returns data.

    python -m options.breeze.sensex_calendar --start 2023-01-01 --end 2026-05-31
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
from datetime import timedelta

import pandas as pd

from options.breeze.config import PROJECT_ROOT, PROBE_DIR
from options.breeze.session import get_client
from options.breeze.throttle import DailyBudgetExhausted, Throttle

OUT_DIR = PROJECT_ROOT / "data" / "BREEZE_OPTIONS" / "BSESEN"
PROGRESS_PATH = OUT_DIR / "sensex_calendar_progress.json"
FMT = "%Y-%m-%dT%H:%M:%S.000Z"
STRIKE_STEP = 100

# Tried in this order for each week. BSE has used all of these at some point.
WEEKDAY_ORDER = [3, 4, 1, 2, 0]   # Thu, Fri, Tue, Wed, Mon


class SensexBuilder:
    def __init__(self):
        self.client = get_client()
        self.throttle = Throttle(verbose=True)

    def fetch_spot(self, start, end) -> pd.DataFrame:
        """Daily SENSEX close — a few calls covers years (1000 candles/request)."""
        print("=" * 70)
        print("1. SENSEX daily spot (BSE cash, 1day)")
        print("=" * 70)

        frames, cursor = [], pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        while cursor <= end_ts:
            chunk_end = min(cursor + pd.Timedelta(days=900), end_ts)
            self.throttle.acquire()
            try:
                resp = self.client.get_historical_data_v2(
                    interval="1day",
                    from_date=cursor.strftime(FMT),
                    to_date=chunk_end.strftime(FMT),
                    stock_code="BSESEN", exchange_code="BSE", product_type="cash",
                )
            except Exception as exc:
                print(f"  [FAIL] {cursor.date()}..{chunk_end.date()}: {exc}")
                cursor = chunk_end + pd.Timedelta(days=1)
                continue

            rows = resp.get("Success") or []
            print(f"  {cursor.date()}..{chunk_end.date()}: {len(rows)} days "
                  f"(Status={resp.get('Status')} {resp.get('Error') or ''})")
            if rows:
                frames.append(pd.DataFrame(rows))
            cursor = chunk_end + pd.Timedelta(days=1)

        if not frames:
            return pd.DataFrame()

        df = pd.concat(frames, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["date"] = df["datetime"].dt.date
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df.dropna(subset=["date", "close"]).drop_duplicates("date")
        return df[["date", "close"]].sort_values("date").reset_index(drop=True)

    def _save_progress(self, found, checked_weeks) -> None:
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROGRESS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "expiries": sorted(str(e) for e in found),
            "checked_weeks": sorted(checked_weeks),
        }), encoding="utf-8")
        tmp.replace(PROGRESS_PATH)
        # Keep the usable CSV current too, so a partial run is still usable.
        if found:
            pd.DataFrame({"expiry_date": sorted(str(e) for e in found)}).to_csv(
                OUT_DIR / "sensex_expiry_calendar.csv", index=False)

    def contract_exists(self, expiry, trade_day, spot) -> tuple[bool, int, int]:
        """Try a few strikes around ATM. Returns (exists, strike, rows)."""
        atm = int(round(spot / STRIKE_STEP) * STRIKE_STEP)
        day = pd.Timestamp(trade_day)
        for strike in (atm, atm + 100, atm - 100, atm + 500, atm - 500):
            self.throttle.acquire()
            try:
                resp = self.client.get_historical_data_v2(
                    interval="1minute",
                    from_date=day.replace(hour=9, minute=20).strftime(FMT),
                    to_date=day.replace(hour=9, minute=40).strftime(FMT),
                    stock_code="BSESEN", exchange_code="BFO", product_type="options",
                    expiry_date=pd.Timestamp(expiry).strftime("%Y-%m-%dT07:00:00.000Z"),
                    right="call", strike_price=str(strike),
                )
            except Exception:
                continue
            rows = len(resp.get("Success") or [])
            if rows:
                return True, strike, rows
        return False, 0, 0

    def discover_expiries(self, spot_df: pd.DataFrame) -> list:
        """Week by week, find which weekday carries the expiry.

        Checkpointed after every week: this burns real budget and takes over an
        hour, so a stop (quota wall, expired token, Ctrl-C) must not throw the
        work away. Re-running skips weeks already settled.
        """
        print("\n" + "=" * 70)
        print("2. Expiry discovery (week by week)")
        print("=" * 70)

        spot_by_date = {r.date: r.close for r in spot_df.itertuples()}
        trading_days = sorted(spot_by_date)
        if not trading_days:
            return []

        # Resume from whatever previous runs established.
        found, checked_weeks = [], set()
        if PROGRESS_PATH.exists():
            try:
                prev = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
                found = [pd.Timestamp(x).date() for x in prev.get("expiries", [])]
                checked_weeks = set(prev.get("checked_weeks", []))
                print(f"  Resuming: {len(found)} expiries known, "
                      f"{len(checked_weeks)} weeks already checked")
            except (json.JSONDecodeError, ValueError):
                pass

        misses = 0
        week_starts = pd.date_range(trading_days[0], trading_days[-1], freq="W-MON")

        try:
            for wk in week_starts:
                monday = wk.date()
                if str(monday) in checked_weeks:
                    continue
                hit = None
                for wd in WEEKDAY_ORDER:
                    cand = monday + timedelta(days=wd)
                    if cand > trading_days[-1]:
                        continue
                    # Need a trading day (with spot) strictly before the expiry.
                    prior = [d for d in trading_days if d < cand]
                    if not prior:
                        continue
                    trade_day = prior[-1]
                    if (cand - trade_day).days > 5:
                        continue

                    ok, strike, rows = self.contract_exists(
                        cand, trade_day, spot_by_date[trade_day])
                    if ok:
                        hit = cand
                        found.append(cand)
                        print(f"  [OK  ] {cand} ({pd.Timestamp(cand).day_name()[:3]}) "
                              f"{strike}CE on {trade_day}: {rows} rows")
                        break
                if hit is None:
                    misses += 1
                    print(f"  [    ] week of {monday}: no expiry found")

                checked_weeks.add(str(monday))
                self._save_progress(found, checked_weeks)
        except DailyBudgetExhausted as exc:
            print(f"\n  {exc}")
        except KeyboardInterrupt:
            print("\n  Interrupted.")

        print(f"\n  Weeks with expiry: {len(found)}   without: {misses}")
        return found


def main() -> int:
    ap = argparse.ArgumentParser(description="Build SENSEX spot + expiry calendar")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-05-31")
    ap.add_argument("--spot-only", action="store_true",
                    help="fetch spot and stop (cheap; also reveals history depth)")
    args = ap.parse_args()

    b = SensexBuilder()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    spot = b.fetch_spot(args.start, args.end)
    if spot.empty:
        print("\n  No SENSEX spot returned — cannot build the calendar.")
        return 1

    spot_path = OUT_DIR / "sensex_spot_daily.csv"
    spot.to_csv(spot_path, index=False)
    print(f"\n  Spot saved: {spot_path}  ({len(spot)} days, "
          f"{spot['date'].min()} -> {spot['date'].max()})")

    if args.spot_only:
        print(f"  Calls used: {b.throttle.used} (left {b.throttle.remaining():,})")
        return 0

    expiries = b.discover_expiries(spot)
    if expiries:
        cal_path = OUT_DIR / "sensex_expiry_calendar.csv"
        pd.DataFrame({"expiry_date": [str(e) for e in sorted(expiries)]}).to_csv(
            cal_path, index=False)
        print(f"  Calendar saved: {cal_path}  ({len(expiries)} expiries)")

        by_dow = pd.Series([pd.Timestamp(e).day_name() for e in expiries]).value_counts()
        print("\n  Expiry weekday distribution:")
        print("   " + by_dow.to_string().replace("\n", "\n   "))

    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    (PROBE_DIR / "sensex_calendar_run.json").write_text(
        json.dumps({"expiries": [str(e) for e in expiries],
                    "spot_days": len(spot),
                    "calls_used": b.throttle.used}, indent=2), encoding="utf-8")
    print(f"\n  Calls used: {b.throttle.used} (left today {b.throttle.remaining():,})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
