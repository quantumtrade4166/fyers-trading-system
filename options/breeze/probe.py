"""Breeze capability probe — run this ONCE before any bulk download.

Four questions the docs don't answer (or answer inconsistently), each of which
changes the download design:

  1. Which intervals actually work for options?  (docs claim 1second..1day)
  2. How far back does NFO options history go?   (determines the date range)
  3. Does BFO / SENSEX work at all?              (docs contradict themselves:
     one page says "BSE and MCX are not available", another shows BFO examples)
  4. How many candles per call, really?          (1000/call cap + 5000 calls/day
     is the binding constraint — this converts directly into download-days)

Probe targets for NIFTY are drawn from the EXISTING dataset
(data/NSE_NIFTY_OPTIONS/), so every request asks for a contract we know traded —
a failure then means "Breeze lacks it", not "bad symbol". It also lets us
cross-check Breeze's OHLC against data we already trust.

Usage:
    python -m options.breeze.probe                # all checks
    python -m options.breeze.probe --skip-sensex  # NFO only
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
import traceback
from datetime import datetime, timedelta

import pandas as pd

from options.breeze.config import DATA_DIR, PROBE_DIR, PROJECT_ROOT
from options.breeze.session import get_client, load_token
from options.breeze.throttle import Throttle

EXISTING_OPTIONS_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS"

# Breeze wants ISO-ish timestamps with a trailing Z. Indian market times are sent
# as naive IST wall-clock in this format (this is what the official examples do).
FMT = "%Y-%m-%dT%H:%M:%S.000Z"


def _iso(dt: datetime) -> str:
    return dt.strftime(FMT)


def _expiry_iso(d) -> str:
    """Expiry timestamps in Breeze examples are dated at 07:00:00.000Z."""
    return pd.Timestamp(d).strftime("%Y-%m-%dT07:00:00.000Z")


class Prober:
    def __init__(self, skip_sensex: bool = False):
        self.client = get_client()
        self.token = load_token()
        self.throttle = Throttle(verbose=True)
        self.skip_sensex = skip_sensex
        self.results: dict = {"run_at": datetime.now().isoformat(timespec="seconds")}

    # ---------- low-level ----------

    def fetch(self, **kwargs) -> dict:
        """One throttled get_historical_data_v2 call, never raising."""
        self.throttle.acquire()
        try:
            resp = self.client.get_historical_data_v2(**kwargs)
        except Exception as exc:  # network / SDK level failure
            return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}",
                    "rows": 0, "data": []}

        status = resp.get("Status")
        data = resp.get("Success") or []
        return {
            "ok": status == 200 and bool(data),
            "status": status,
            "error": resp.get("Error"),
            "rows": len(data),
            "data": data,
        }

    # ---------- probe targets from existing data ----------

    def _pick_known_contract(self, year: int) -> dict | None:
        """Find a contract/day we KNOW traded, from the existing parquet dataset."""
        path = EXISTING_OPTIONS_DIR / str(year) / "ohlcv_1min.parquet"
        if not path.exists():
            return None
        try:
            df = pd.read_parquet(path, columns=["datetime", "strike_price",
                                                "option_type", "date", "volume"])
        except Exception:
            return None
        if df.empty:
            return None

        # ATM-ish liquid contract: pick from the busiest (date, strike, type).
        busiest = (df.groupby(["date", "strike_price", "option_type"])["volume"]
                     .sum().sort_values(ascending=False))
        if busiest.empty:
            return None

        # Prefer a non-expiry-day target — expiry day is the busiest but is also
        # the most likely to behave differently on the API side.
        fallback = None
        for (day, strike, right) in busiest.index[:50]:
            expiry = self._expiry_for(pd.Timestamp(day).date())
            if expiry is None:
                continue
            target = {
                "trade_date": str(day),
                "strike_price": int(strike),
                "right": "call" if str(right).upper().startswith("C") else "put",
                "expiry_date": str(expiry),
            }
            if str(expiry) != str(day):
                return target
            fallback = fallback or target
        return fallback

    def _expiry_for(self, trade_date):
        """First expiry on/after trade_date, from the empirical expiry calendar."""
        cal_path = EXISTING_OPTIONS_DIR / "expiry_calendar.csv"
        if not hasattr(self, "_calendar"):
            if not cal_path.exists():
                self._calendar = []
            else:
                self._calendar = sorted(
                    pd.read_csv(cal_path)["expiry_date"].apply(
                        lambda x: pd.Timestamp(x).date()).tolist()
                )
        for exp in self._calendar:
            if exp >= trade_date:
                return exp
        return None

    # ---------- checks ----------

    def check_intervals(self) -> None:
        """Which intervals return data for a known-good NIFTY option contract?"""
        print("\n" + "=" * 70)
        print("1. INTERVAL SUPPORT (NIFTY options, NFO)")
        print("=" * 70)

        target = self._pick_known_contract(2026) or self._pick_known_contract(2025)
        if not target:
            print("  SKIPPED — no existing dataset to draw a known contract from.")
            self.results["intervals"] = {"skipped": True}
            return

        print(f"  Probe contract: NIFTY {target['strike_price']} {target['right'].upper()} "
              f"exp {target['expiry_date']} on {target['trade_date']}")

        day = pd.Timestamp(target["trade_date"])
        out = {}
        for interval in ("1second", "1minute", "5minute", "30minute", "1day"):
            # A 15-minute slice keeps even 1second under the 1000-candle cap.
            res = self.fetch(
                interval=interval,
                from_date=_iso(day.replace(hour=9, minute=15, second=0)),
                to_date=_iso(day.replace(hour=9, minute=30, second=0)),
                stock_code="NIFTY",
                exchange_code="NFO",
                product_type="options",
                expiry_date=_expiry_iso(target["expiry_date"]),
                right=target["right"],
                strike_price=str(target["strike_price"]),
            )
            mark = "OK  " if res["ok"] else "FAIL"
            print(f"  [{mark}] {interval:<10} rows={res['rows']:<5} "
                  f"status={res['status']} {res['error'] or ''}")
            out[interval] = {k: res[k] for k in ("ok", "status", "error", "rows")}
            if res["ok"] and interval == "1second":
                sample = res["data"][0]
                print(f"         sample keys: {sorted(sample.keys())}")
                out["1second_sample"] = sample

        self.results["intervals"] = out

    def check_second_depth(self) -> None:
        """KEY QUESTION 1 — how far back does 1-SECOND options data go?

        Interval support and history depth are independent: Breeze may serve
        1-minute back to 2021 but 1-second only for recent months. Probed
        separately, year by year, on contracts we know traded.
        """
        print("\n" + "=" * 70)
        print("KEY QUESTION 1: how far back is 1-SECOND NIFTY options data?")
        print("=" * 70)

        out = {}
        for year in range(2026, 2020, -1):
            target = self._pick_known_contract(year)
            if not target:
                out[year] = {"probed": False, "reason": "no local reference data"}
                print(f"  {year}: (no local reference data — not probed)")
                continue

            day = pd.Timestamp(target["trade_date"])
            res = self.fetch(
                interval="1second",
                from_date=_iso(day.replace(hour=9, minute=15, second=0)),
                to_date=_iso(day.replace(hour=9, minute=25, second=0)),  # 600s < 1000 cap
                stock_code="NIFTY",
                exchange_code="NFO",
                product_type="options",
                expiry_date=_expiry_iso(target["expiry_date"]),
                right=target["right"],
                strike_price=str(target["strike_price"]),
            )
            mark = "OK  " if res["ok"] else "FAIL"
            print(f"  [{mark}] {year}: {target['trade_date']} "
                  f"{target['strike_price']}{target['right'][0].upper()} "
                  f"rows={res['rows']:<5} {res['error'] or ''}")
            out[year] = {"probed": True, "target": target,
                         **{k: res[k] for k in ("ok", "status", "error", "rows")}}

        good = sorted(y for y, v in out.items() if v.get("ok"))
        if good:
            print(f"\n  -> 1-second data confirmed back to {min(good)}")
        else:
            print("\n  -> NO 1-second data returned for any year probed.")
        self.results["second_depth"] = out

    def check_next_week_expiry(self) -> None:
        """KEY QUESTION 2 — is NEXT-WEEK expiry available?

        This is the gap that has Supertrend Credit Spread parked: the current
        dataset is front-week only, so 72% of its signals (0-3 DTE) can't be
        entered. Probe asks for the SECOND expiry after each trade date — i.e.
        a contract the existing dataset does not contain.
        """
        print("\n" + "=" * 70)
        print("KEY QUESTION 2: is NEXT-WEEK expiry available?")
        print("=" * 70)

        spot_by_date = self._spot_by_date()
        if not spot_by_date:
            print("  SKIPPED — no spot reference data.")
            self.results["next_week"] = {"skipped": True}
            return

        out = {"attempts": []}
        for year in range(2026, 2020, -1):
            probe_day = self._mid_year_trading_day(spot_by_date, year)
            if probe_day is None:
                print(f"  {year}: (no local reference data — not probed)")
                continue

            day_date = pd.Timestamp(probe_day).date()
            upcoming = [e for e in self._expiries() if e > day_date][:2]
            if len(upcoming) < 2:
                continue
            front, nxt = upcoming[0], upcoming[1]

            atm = int(round(spot_by_date[probe_day] / 50) * 50)
            day = pd.Timestamp(probe_day)

            row = {"year": year, "trade_date": probe_day, "front_expiry": str(front),
                   "next_expiry": str(nxt), "strike": atm,
                   "dte_next": (nxt - day_date).days}

            for label, expiry in (("front", front), ("next", nxt)):
                res = self.fetch(
                    interval="1minute",
                    from_date=_iso(day.replace(hour=9, minute=20, second=0)),
                    to_date=_iso(day.replace(hour=9, minute=40, second=0)),
                    stock_code="NIFTY",
                    exchange_code="NFO",
                    product_type="options",
                    expiry_date=_expiry_iso(expiry),
                    right="call",
                    strike_price=str(atm),
                )
                row[label] = {k: res[k] for k in ("ok", "status", "error", "rows")}

            front_ok = row["front"]["ok"]
            next_ok = row["next"]["ok"]
            print(f"  {year} {probe_day} ATM {atm}CE: "
                  f"front({front}) {'OK' if front_ok else 'FAIL'} rows={row['front']['rows']:<4} | "
                  f"next({nxt}, {row['dte_next']}d) {'OK' if next_ok else 'FAIL'} "
                  f"rows={row['next']['rows']}")
            if not next_ok and row["next"]["error"]:
                print(f"       next-week error: {row['next']['error']}")
            out["attempts"].append(row)

        works = [a for a in out["attempts"] if a["next"]["ok"]]
        out["works"] = bool(works)
        if works:
            years = sorted(a["year"] for a in works)
            print(f"\n  -> NEXT-WEEK expiry AVAILABLE ({min(years)}-{max(years)}). "
                  "This unblocks Supertrend Credit Spread.")
        else:
            print("\n  -> Next-week expiry NOT returned. Breeze may be front-week only too.")
        self.results["next_week"] = out

    # -- helpers for the two key questions --

    def _expiries(self) -> list:
        self._expiry_for(datetime.now().date())  # ensures calendar is loaded
        return self._calendar

    def _spot_by_date(self) -> dict:
        if hasattr(self, "_spot_cache"):
            return self._spot_cache
        spot = {}
        for year_dir in sorted(EXISTING_OPTIONS_DIR.glob("[0-9][0-9][0-9][0-9]")):
            parquet = year_dir / "ohlcv_1min.parquet"
            if not parquet.exists():
                continue
            df = pd.read_parquet(parquet, columns=["date", "spot"])
            for day, value in df.groupby("date")["spot"].first().items():
                spot[str(day)] = float(value)
        self._spot_cache = spot
        return spot

    def _mid_year_trading_day(self, spot_by_date: dict, year: int):
        """A mid-year Monday-ish day, well clear of expiry, for a clean 2-expiry test."""
        days = sorted(d for d in spot_by_date if d.startswith(str(year)))
        if not days:
            return None
        return days[len(days) // 2]

    def check_history_depth(self) -> None:
        """Walk back year by year — where does NFO options history stop?"""
        print("\n" + "=" * 70)
        print("2. HISTORY DEPTH (how far back do NIFTY options go?)")
        print("=" * 70)

        out = {}
        for year in range(2026, 2015, -1):
            target = self._pick_known_contract(year)
            if not target:
                # No local data for this year to build a known-good target from.
                out[year] = {"probed": False, "reason": "no local reference data"}
                print(f"  {year}: (no local reference data — not probed)")
                continue

            day = pd.Timestamp(target["trade_date"])
            res = self.fetch(
                interval="1minute",
                from_date=_iso(day.replace(hour=9, minute=15, second=0)),
                to_date=_iso(day.replace(hour=15, minute=30, second=0)),
                stock_code="NIFTY",
                exchange_code="NFO",
                product_type="options",
                expiry_date=_expiry_iso(target["expiry_date"]),
                right=target["right"],
                strike_price=str(target["strike_price"]),
            )
            mark = "OK  " if res["ok"] else "FAIL"
            print(f"  [{mark}] {year}: {target['trade_date']} "
                  f"{target['strike_price']}{target['right'][0].upper()} "
                  f"rows={res['rows']:<5} {res['error'] or ''}")
            out[year] = {"probed": True, "target": target,
                         **{k: res[k] for k in ("ok", "status", "error", "rows")}}

        self.results["history_depth"] = out

    def check_sensex(self) -> None:
        """Does BFO/SENSEX work? Docs disagree, so try several stock_code spellings."""
        print("\n" + "=" * 70)
        print("3. SENSEX / BFO SUPPORT")
        print("=" * 70)
        if self.skip_sensex:
            print("  SKIPPED (--skip-sensex)")
            self.results["sensex"] = {"skipped": True}
            return

        # Recent Tuesdays and Thursdays — SENSEX weekly expiry day has moved around,
        # so probe both weekdays over the last few weeks and see what returns data.
        today = datetime.now().date()
        candidates = []
        for back in range(7, 45):
            d = today - timedelta(days=back)
            if d.weekday() in (1, 3):  # Tue, Thu
                candidates.append(d)
        candidates = candidates[:6]

        out = {"attempts": []}
        found = False
        for stock_code in ("BSESEN", "SENSEX"):
            for exp in candidates:
                trade_day = exp - timedelta(days=1)
                if trade_day.weekday() >= 5:
                    continue
                # Strike must be a real one; SENSEX strikes step by 100.
                for strike in self._sensex_strike_guesses():
                    res = self.fetch(
                        interval="1minute",
                        from_date=_iso(datetime.combine(trade_day, datetime.min.time())
                                       .replace(hour=9, minute=20)),
                        to_date=_iso(datetime.combine(trade_day, datetime.min.time())
                                     .replace(hour=9, minute=40)),
                        stock_code=stock_code,
                        exchange_code="BFO",
                        product_type="options",
                        expiry_date=_expiry_iso(exp),
                        right="call",
                        strike_price=str(strike),
                    )
                    out["attempts"].append({
                        "stock_code": stock_code, "expiry": str(exp),
                        "trade_date": str(trade_day), "strike": strike,
                        **{k: res[k] for k in ("ok", "status", "error", "rows")},
                    })
                    if res["ok"]:
                        print(f"  [OK  ] BFO {stock_code} {strike}CE exp {exp} "
                              f"on {trade_day} rows={res['rows']}")
                        out["works"] = True
                        out["working_example"] = out["attempts"][-1]
                        found = True
                        break
                if found:
                    break
            if found:
                break

        if not found:
            print("  [FAIL] No BFO/SENSEX combination returned data.")
            print("         Last error:",
                  out["attempts"][-1]["error"] if out["attempts"] else "n/a")
            print("         -> SENSEX options likely unavailable on Breeze; NIFTY only.")
            out["works"] = False

        self.results["sensex"] = out

    def _sensex_strike_guesses(self) -> list[int]:
        """SENSEX round-number strikes near current levels (no local spot data)."""
        return [82000, 81000, 80000, 83000, 79000]

    def check_call_cost(self) -> None:
        """Measure candles-per-call so we can project total download time."""
        print("\n" + "=" * 70)
        print("4. CALL COST (candles per request -> download-days)")
        print("=" * 70)

        target = self._pick_known_contract(2026) or self._pick_known_contract(2025)
        if not target:
            print("  SKIPPED — no reference contract.")
            self.results["call_cost"] = {"skipped": True}
            return

        day = pd.Timestamp(target["trade_date"])
        out = {}
        for interval in ("1minute", "1second"):
            res = self.fetch(
                interval=interval,
                from_date=_iso(day.replace(hour=9, minute=15, second=0)),
                to_date=_iso(day.replace(hour=15, minute=30, second=0)),
                stock_code="NIFTY",
                exchange_code="NFO",
                product_type="options",
                expiry_date=_expiry_iso(target["expiry_date"]),
                right=target["right"],
                strike_price=str(target["strike_price"]),
            )
            capped = res["rows"] >= 1000
            print(f"  {interval:<9} full trading day -> rows={res['rows']} "
                  f"{'(HIT 1000 CAP — needs windowing)' if capped else ''}")
            out[interval] = {"rows": res["rows"], "hit_cap": capped,
                             "status": res["status"], "error": res["error"]}

        self.results["call_cost"] = out
        self._project(out)

    def _project(self, cost: dict) -> None:
        """Turn measured call cost into a realistic download-time estimate."""
        print("\n  Projected download cost (ATM+/-10 CE+PE = 42 contracts/expiry):")
        trading_days_per_year = 250
        for interval, info in cost.items():
            rows = info.get("rows") or 0
            if not rows:
                continue
            calls_per_contract_day = max(1, -(-rows // 1000))  # ceil
            for n_expiries, label in ((1, "front week only"), (2, "front + next week")):
                contracts = 42 * n_expiries
                per_day = contracts * calls_per_contract_day
                days_of_data_per_budget = 4950 / per_day
                for years in (1, 5):
                    total_days = trading_days_per_year * years
                    download_days = total_days / days_of_data_per_budget
                    print(f"    {interval:<9} {label:<18} {years}yr -> "
                          f"{per_day:>5} calls/data-day, "
                          f"~{download_days:>6.1f} days of downloading")

    # ---------- report ----------

    def save(self) -> None:
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
        path = PROBE_DIR / f"probe_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps(self.results, indent=2, default=str), encoding="utf-8")
        print(f"\nProbe results saved: {path}")

    def summary(self) -> None:
        print("\n" + "=" * 70)
        print("SUMMARY — what this means for the download plan")
        print("=" * 70)

        iv = self.results.get("intervals", {})
        if not iv.get("skipped"):
            ok = [k for k, v in iv.items() if isinstance(v, dict) and v.get("ok")]
            print(f"  Intervals available : {', '.join(ok) if ok else 'NONE'}")
            print(f"  1-second data       : {'YES' if iv.get('1second', {}).get('ok') else 'NO'}")

        sec = self.results.get("second_depth", {})
        sec_ok = sorted(y for y, v in sec.items() if isinstance(v, dict) and v.get("ok"))
        if sec_ok:
            print(f"  1-second history    : back to {min(sec_ok)}")
        elif sec:
            print("  1-second history    : NONE returned")

        nw = self.results.get("next_week", {})
        if not nw.get("skipped"):
            print(f"  Next-week expiry    : {'AVAILABLE' if nw.get('works') else 'NOT AVAILABLE'}")

        depth = self.results.get("history_depth", {})
        good = sorted(y for y, v in depth.items()
                      if isinstance(v, dict) and v.get("ok"))
        if good:
            print(f"  History confirmed   : {min(good)} to {max(good)}")

        sx = self.results.get("sensex")
        if sx is None:
            print("  SENSEX / BFO        : not tested (run without --quick)")
        elif sx.get("skipped"):
            print("  SENSEX / BFO        : skipped")
        else:
            print(f"  SENSEX / BFO        : {'WORKS' if sx.get('works') else 'NOT AVAILABLE'}")

        print(f"  Calls used by probe : {self.throttle.used} "
              f"({self.throttle.remaining()} left today)")

    def run(self, quick: bool = False) -> None:
        self.check_intervals()
        self.check_second_depth()
        self.check_next_week_expiry()
        if not quick:
            self.check_history_depth()
            self.check_sensex()
            self.check_call_cost()
        self.summary()
        self.save()


def main() -> int:
    ap = argparse.ArgumentParser(description="Breeze capability probe")
    ap.add_argument("--skip-sensex", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="only the two key questions: 1-second depth + next-week expiry")
    args = ap.parse_args()

    try:
        prober = Prober(skip_sensex=args.skip_sensex)
    except RuntimeError as exc:
        print(f"\n{exc}\n")
        return 1

    try:
        prober.run(quick=args.quick)
    except Exception:
        traceback.print_exc()
        prober.save()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
