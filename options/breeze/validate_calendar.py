"""Validate (and repair) the expiry calendar using Breeze as ground truth.

The existing calendar in data/NSE_NIFTY_OPTIONS/expiry_calendar.csv was derived
empirically from chain-wide OI craters. That heuristic produces false positives:
the probe found 2022-06-24 (Friday) and 2025-06-24 (Tuesday) listed as expiries,
and Breeze returns no data for either — while the neighbouring real expiries work.

Bad dates cost twice in a bulk download: calls spent on contracts that don't
exist, and the real expiry never fetched. So validate before downloading.

Method: for a candidate expiry E, ask Breeze for an ATM contract a couple of
days before E. Data back => E is a real expiry.

    python -m options.breeze.validate_calendar --suspicious   # 45 off-weekday dates
    python -m options.breeze.validate_calendar --all          # every date (~290 calls)
    python -m options.breeze.validate_calendar --suspicious --repair
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

CAL_PATH = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS" / "expiry_calendar.csv"
OPTIONS_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS"
SWITCH = pd.Timestamp("2025-09-02").date()   # NSE weekly expiry Thu -> Tue
FMT = "%Y-%m-%dT%H:%M:%S.000Z"


def expected_weekday(d) -> int:
    return 1 if d >= SWITCH else 3          # Tuesday=1, Thursday=3


def load_spot() -> dict:
    spot = {}
    for year_dir in sorted(OPTIONS_DIR.glob("[0-9][0-9][0-9][0-9]")):
        p = year_dir / "ohlcv_1min.parquet"
        if p.exists():
            df = pd.read_parquet(p, columns=["date", "spot"])
            for day, val in df.groupby("date")["spot"].first().items():
                spot[str(day)] = float(val)
    return spot


class Validator:
    def __init__(self, ignore_daily_cap: bool = False):
        self.client = get_client()
        self.throttle = Throttle(verbose=True, ignore_daily_cap=ignore_daily_cap)
        self.spot = load_spot()
        self.trading_days = sorted(self.spot)
        self.cache: dict = {}

    def _trade_day_before(self, expiry, max_back: int = 6):
        """Latest trading day strictly before expiry (needs spot for the ATM strike)."""
        target = str(expiry)
        candidates = [d for d in self.trading_days if d < target]
        if not candidates:
            return None
        day = candidates[-1]
        if (pd.Timestamp(expiry).date() - pd.Timestamp(day).date()).days > max_back:
            return None
        return day

    def exists(self, expiry) -> tuple[bool, str]:
        """Does Breeze serve any contract for this expiry? (cached)"""
        key = str(expiry)
        if key in self.cache:
            return self.cache[key]

        trade_day = self._trade_day_before(expiry)
        if trade_day is None:
            result = (False, "no trading day with spot data before this expiry")
            self.cache[key] = result
            return result

        atm = int(round(self.spot[trade_day] / 50) * 50)
        day = pd.Timestamp(trade_day)

        # Try ATM, then a couple of neighbours — guards against a strike that
        # genuinely never traded rather than a missing expiry.
        for strike in (atm, atm + 50, atm - 50):
            self.throttle.acquire()
            try:
                resp = self.client.get_historical_data_v2(
                    interval="1minute",
                    from_date=day.replace(hour=9, minute=20).strftime(FMT),
                    to_date=day.replace(hour=9, minute=40).strftime(FMT),
                    stock_code="NIFTY", exchange_code="NFO", product_type="options",
                    expiry_date=pd.Timestamp(expiry).strftime("%Y-%m-%dT07:00:00.000Z"),
                    right="call", strike_price=str(strike),
                )
            except Exception as exc:
                result = (False, f"{type(exc).__name__}: {exc}")
                self.cache[key] = result
                return result

            if resp.get("Status") == 200 and resp.get("Success"):
                result = (True, f"{len(resp['Success'])} rows @ {strike}CE on {trade_day}")
                self.cache[key] = result
                return result

        result = (False, f"no data for ATM+/-50 on {trade_day}")
        self.cache[key] = result
        return result

    def nearby_candidates(self, bad_expiry) -> list:
        """Plausible true expiries in the same week as a rejected date."""
        d = pd.Timestamp(bad_expiry).date()
        want = expected_weekday(d)
        out = []
        for delta in range(-4, 5):
            cand = d + timedelta(days=delta)
            if cand == d:
                continue
            # the expected weekday, plus adjacent days for holiday shifts
            if cand.weekday() == want or abs(delta) <= 2:
                if cand.weekday() < 5:
                    out.append(cand)
        # nearest first
        return sorted(set(out), key=lambda c: abs((c - d).days))[:4]


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate expiry calendar against Breeze")
    ap.add_argument("--suspicious", action="store_true",
                    help="only off-weekday dates (default)")
    ap.add_argument("--all", action="store_true", help="every date in the calendar")
    ap.add_argument("--repair", action="store_true",
                    help="write a corrected calendar CSV")
    ap.add_argument("--limit", type=int, default=0, help="cap dates checked (testing)")
    ap.add_argument("--skip-validated", action="store_true",
                    help="skip dates already settled in a previous run")
    ap.add_argument("--find-missing", action="store_true",
                    help="test expected-weekday dates ABSENT from the calendar")
    ap.add_argument("--ignore-daily-cap", action="store_true",
                    help="do not self-limit at 5000/day (ICICI does not enforce it)")
    args = ap.parse_args()

    cal = pd.read_csv(CAL_PATH)
    cal["d"] = pd.to_datetime(cal["expiry_date"]).dt.date
    cal["odd"] = [d.weekday() != expected_weekday(d) for d in cal["d"]]

    prior_path = PROBE_DIR / "calendar_validation.json"
    prior = (json.loads(prior_path.read_text(encoding="utf-8"))
             if prior_path.exists() else {"confirmed": [], "rejected": [], "replacements": {}})

    if args.find_missing:
        # Every expected-weekday date in the covered span that the calendar omits.
        known = {str(d) for d in cal["d"]}
        known |= set(prior.get("replacements", {}).values())
        lo, hi = min(cal["d"]), max(cal["d"])
        targets, day = [], lo
        while day <= hi:
            if day.weekday() == expected_weekday(day) and str(day) not in known:
                targets.append(day)
            day += timedelta(days=1)
        print(f"(find-missing: {len(targets)} expected-weekday dates absent from calendar)")
    else:
        targets = cal["d"].tolist() if args.all else cal.loc[cal["odd"], "d"].tolist()

    if args.skip_validated:
        settled = set(prior.get("confirmed", [])) | set(prior.get("rejected", []))
        before = len(targets)
        targets = [d for d in targets if str(d) not in settled]
        print(f"(skipping {before - len(targets)} already-validated dates)")

    if args.limit:
        targets = targets[: args.limit]

    print("=" * 74)
    print(f"Validating {len(targets)} expiry dates against Breeze "
          f"({'ALL' if args.all else 'off-weekday only'})")
    print("=" * 74)

    v = Validator(ignore_daily_cap=args.ignore_daily_cap)
    confirmed, rejected, replacements = [], [], {}

    try:
        for i, exp in enumerate(targets, 1):
            ok, note = v.exists(exp)
            dow = pd.Timestamp(exp).day_name()[:3]
            if ok:
                confirmed.append(exp)
                print(f"  [REAL] {exp} {dow}  {note}")
            else:
                rejected.append(exp)
                # In find-missing mode a miss just means "holiday / not an expiry",
                # which is the expected outcome — no point hunting neighbours.
                if args.find_missing:
                    continue
                print(f"  [BAD ] {exp} {dow}  {note}")
                for cand in v.nearby_candidates(exp):
                    ok2, note2 = v.exists(cand)
                    if ok2:
                        replacements[str(exp)] = str(cand)
                        print(f"         -> real expiry that week: {cand} "
                              f"({pd.Timestamp(cand).day_name()[:3]})  {note2}")
                        break
            if i % 10 == 0:
                print(f"    ... {i}/{len(targets)}  budget left {v.throttle.remaining():,}")
    except DailyBudgetExhausted as exc:
        print(f"\n  {exc}")
    except KeyboardInterrupt:
        print("\n  Interrupted.")

    print("\n" + "=" * 74)
    print(f"  Confirmed real : {len(confirmed)}")
    print(f"  Rejected       : {len(rejected)}")
    print(f"  Replacements   : {len(replacements)}")
    print(f"  Calls used     : {v.throttle.used}  (left today {v.throttle.remaining():,})")

    # Merge with anything settled in earlier runs so the record accumulates.
    merged = {
        "confirmed": sorted(set(prior.get("confirmed", [])) | {str(x) for x in confirmed}),
        "rejected": sorted(set(prior.get("rejected", [])) | {str(x) for x in rejected}),
        "replacements": {**prior.get("replacements", {}), **replacements},
    }
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    out = PROBE_DIR / "calendar_validation.json"
    out.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print(f"  Saved          : {out}  "
          f"(cumulative: {len(merged['confirmed'])} real, {len(merged['rejected'])} bad)")

    if args.repair:
        good = {str(d) for d in cal["d"]} - set(merged["rejected"])
        good |= set(merged["replacements"].values())
        good |= set(merged["confirmed"])
        fixed = sorted(good)
        dest = OPTIONS_DIR / "expiry_calendar_validated.csv"
        pd.DataFrame({"expiry_date": fixed}).to_csv(dest, index=False)
        print(f"  Repaired CSV   : {dest}  ({len(fixed)} dates)")
        print("  NOTE: original left untouched. Diff before adopting it.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
