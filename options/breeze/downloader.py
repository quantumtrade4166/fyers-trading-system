"""Bulk options history downloader for Breeze.

Design notes — these follow from the two hard API limits (1000 candles/request,
5000 calls/day):

  * Every contract-day is one manifest entry. The manifest is written after each
    entry, so a run that dies (budget exhausted, token expired, VPS reboot)
    resumes exactly where it stopped instead of re-spending calls.
  * Requests are auto-windowed: a range that would exceed 1000 candles is split
    until it fits. This is what makes 1second and 1minute share one code path.
  * Rows are buffered and flushed per year-partition, matching the layout of the
    existing dataset (data/NSE_NIFTY_OPTIONS/{year}/ohlcv_1min.parquet) so the
    backtest loaders can read both without special-casing.

Usage:
    # what a run would cost, without spending a single call
    python -m options.breeze.downloader --dry-run --start 2021-01-01 --end 2026-05-31

    # the real thing (resumable — just re-run it each day)
    python -m options.breeze.downloader --start 2024-01-01 --end 2026-05-31 \
        --interval 1minute --expiries 2 --strikes 10
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from options.breeze.config import (
    DATA_DIR,
    MANIFEST_PATH,
    MAX_CANDLES_PER_REQUEST,
    PROJECT_ROOT,
)
from options.breeze.session import get_client
from options.breeze.throttle import DailyBudgetExhausted, Throttle

EXISTING_OPTIONS_DIR = PROJECT_ROOT / "data" / "NSE_NIFTY_OPTIONS"
FMT = "%Y-%m-%dT%H:%M:%S.000Z"

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

# Candles a full trading day yields, per interval — used for window sizing.
CANDLES_PER_DAY = {
    "1second": 22_500,
    "1minute": 375,
    "5minute": 75,
    "30minute": 13,
    "1day": 1,
}

STRIKE_STEP = {"NIFTY": 50, "BSESEN": 100}

# Phrases ICICI returns when the account's API quota is spent. Detected so the
# run stops at the wall and reports it, instead of burning through the remaining
# targets recording thousands of identical "errors".
RATE_LIMIT_MARKERS = (
    "limit exceed",
    "api call per day",
    "calls per day",
    "rate limit",
    "too many requests",
    "quota",
)


class RateLimitHit(RuntimeError):
    """The server refused a call because the account's quota is spent."""


class SuspiciousEmptyRun(RuntimeError):
    """Too many consecutive empty responses.

    A quota wall that surfaces as HTTP 200 + empty payload (rather than an
    explicit error) is indistinguishable from a genuinely untraded strike — and
    would silently mark thousands of contract-days 'empty' so the resume never
    retries them. Real empties are interspersed with data, so a long unbroken run
    of them means something is wrong, not that the market was quiet.
    """


# Transient network failures: retry the same request before giving up on it.
RETRY_ATTEMPTS = 4
RETRY_BACKOFF = (2, 8, 20)        # seconds between attempts

# If this many contract-days fail in a row, assume the IP is being throttled and
# wait it out instead of burning through the remaining targets marking errors.
NET_ERRORS_BEFORE_PAUSE = 10
NETWORK_PAUSE_SECONDS = 600

# Failure signatures worth retrying. Anything else is a real API response.
TRANSIENT_MARKERS = (
    "nameresolution", "temporary failure in name resolution",
    "max retries exceeded", "connection reset", "connection aborted",
    "unexpected_eof", "sslerror", "ssleoferror", "timed out", "timeout",
    "remotedisconnected", "connectionerror", "bad gateway", "503", "502",
)


def _is_transient(error: str) -> bool:
    low = (error or "").lower()
    return any(marker in low for marker in TRANSIENT_MARKERS)


# Consecutive empty contract-days tolerated before assuming something is wrong.
# A full data-day is 84 contracts; legitimate far-OTM empties never run this long
# unbroken across both expiries and both rights.
MAX_CONSECUTIVE_EMPTY = 60


@dataclass(frozen=True)
class Contract:
    trade_date: str
    expiry_date: str
    strike_price: int
    right: str          # "call" | "put"
    stock_code: str
    exchange_code: str

    def key(self, interval: str) -> str:
        return (f"{self.exchange_code}|{self.stock_code}|{self.trade_date}|"
                f"{self.expiry_date}|{self.strike_price}|{self.right}|{interval}")


class Manifest:
    """Resume state. One entry per contract-day."""

    def __init__(self, path: Path = MANIFEST_PATH):
        self.path = path
        self.entries: dict = {}
        if path.exists():
            try:
                self.entries = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"  [warn] manifest unreadable, starting fresh: {path}")

    def done(self, key: str) -> bool:
        entry = self.entries.get(key)
        # Retry anything that previously errored; skip completed work (including
        # confirmed-empty contracts, which are legitimately no-data).
        return bool(entry) and entry.get("status") in ("ok", "empty")

    def record(self, key: str, status: str, rows: int = 0, error: str = "") -> None:
        self.entries[key] = {"status": status, "rows": rows,
                             "at": datetime.now().isoformat(timespec="seconds")}
        if error:
            self.entries[key]["error"] = error[:300]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries), encoding="utf-8")
        tmp.replace(self.path)

    def stats(self) -> dict:
        out = {"ok": 0, "empty": 0, "error": 0}
        for entry in self.entries.values():
            out[entry.get("status", "error")] = out.get(entry.get("status", "error"), 0) + 1
        return out


def _display(path: Path) -> str:
    """Repo-relative path when possible — DATA_DIR can be pointed outside the repo."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _iso(dt: datetime) -> str:
    return dt.strftime(FMT)


def _expiry_iso(d) -> str:
    return pd.Timestamp(d).strftime("%Y-%m-%dT07:00:00.000Z")


SENSEX_DIR = DATA_DIR / "BSESEN"


def load_expiry_calendar(stock_code: str = "NIFTY") -> list:
    """Expiry dates. NIFTY comes from the (Breeze-validated) local calendar;
    SENSEX from the one discovered by options.breeze.sensex_calendar."""
    if stock_code == "BSESEN":
        path = SENSEX_DIR / "sensex_expiry_calendar.csv"
        hint = "Build it first:  python -m options.breeze.sensex_calendar"
    else:
        path = EXISTING_OPTIONS_DIR / "expiry_calendar.csv"
        hint = "It is produced by backtesting/options_credit_spread/expiry_calendar.py"

    if not path.exists():
        raise RuntimeError(f"Expiry calendar not found: {path}\n{hint}")
    return sorted(pd.read_csv(path)["expiry_date"]
                  .apply(lambda x: pd.Timestamp(x).date()).tolist())


def load_spot_by_date(stock_code: str = "NIFTY") -> dict:
    """Daily spot, used to centre the strike window."""
    spot: dict = {}

    if stock_code == "BSESEN":
        path = SENSEX_DIR / "sensex_spot_daily.csv"
        if not path.exists():
            raise RuntimeError(
                f"SENSEX spot series not found: {path}\n"
                "Build it first:  python -m options.breeze.sensex_calendar --spot-only"
            )
        df = pd.read_csv(path)
        for row in df.itertuples():
            spot[str(row.date)] = float(row.close)
        return spot

    for year_dir in sorted(EXISTING_OPTIONS_DIR.glob("[0-9][0-9][0-9][0-9]")):
        parquet = year_dir / "ohlcv_1min.parquet"
        if not parquet.exists():
            continue
        df = pd.read_parquet(parquet, columns=["date", "spot"])
        for day, value in df.groupby("date")["spot"].first().items():
            spot[str(day)] = float(value)
    return spot


class Downloader:
    def __init__(self, args):
        self.args = args
        self.interval = args.interval
        self.stock_code = args.stock_code
        self.exchange_code = args.exchange_code
        self.strike_step = STRIKE_STEP.get(self.stock_code, 50)

        self.manifest = Manifest()
        from options.breeze.config import MAX_CALLS_PER_MINUTE
        self.throttle = Throttle(
            verbose=True,
            ignore_daily_cap=getattr(args, "ignore_daily_cap", False),
            per_minute=getattr(args, "per_minute", None) or MAX_CALLS_PER_MINUTE,
        )
        self.client = None if args.dry_run else get_client()

        self.buffer: list = []
        # Manifest entries for rows still sitting in the buffer. They are only
        # committed AFTER the parquet write succeeds — otherwise a crash between
        # "recorded done" and "written to disk" would make the resume skip
        # contract-days whose rows were lost, leaving silent gaps.
        self.pending: list[tuple] = []
        self.rows_written = 0
        self.contracts_done = 0
        self.consecutive_empty = 0
        self.hit_wall_at = None
        self.started = time.time()

    # ---------- target enumeration ----------

    def build_targets(self) -> list[Contract]:
        expiries = load_expiry_calendar(self.stock_code)
        spot_by_date = load_spot_by_date(self.stock_code)

        start = pd.Timestamp(self.args.start).date()
        end = pd.Timestamp(self.args.end).date()

        targets: list[Contract] = []
        for day_str, spot in sorted(spot_by_date.items()):
            day = pd.Timestamp(day_str).date()
            if not (start <= day <= end):
                continue

            upcoming = [e for e in expiries if e >= day][: self.args.expiries]
            if not upcoming:
                continue

            atm = round(spot / self.strike_step) * self.strike_step
            strikes = [int(atm + i * self.strike_step)
                       for i in range(-self.args.strikes, self.args.strikes + 1)]

            for expiry in upcoming:
                for strike in strikes:
                    for right in ("call", "put"):
                        targets.append(Contract(
                            trade_date=str(day), expiry_date=str(expiry),
                            strike_price=strike, right=right,
                            stock_code=self.stock_code,
                            exchange_code=self.exchange_code,
                        ))
        return targets

    # ---------- fetching ----------

    def _windows(self, day) -> list[tuple[datetime, datetime]]:
        """Split a trading day into ranges that stay under the 1000-candle cap."""
        base = pd.Timestamp(day).to_pydatetime()
        open_dt = base.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0)
        close_dt = base.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0)

        per_day = CANDLES_PER_DAY.get(self.interval, 375)
        n_windows = max(1, -(-per_day // MAX_CANDLES_PER_REQUEST))  # ceil
        if n_windows == 1:
            return [(open_dt, close_dt)]

        total = (close_dt - open_dt).total_seconds()
        step = total / n_windows
        return [(open_dt + timedelta(seconds=step * i),
                 open_dt + timedelta(seconds=step * (i + 1)))
                for i in range(n_windows)]

    def _fetch_range(self, contract: Contract, win_start: datetime,
                     win_end: datetime) -> tuple[str, list, str]:
        """One raw request for a time range, with retries on transient failures.

        Network errors (DNS, SSL EOF, connection reset) are transient — often the
        server briefly refusing a burst. Without retries a 60-second blip marks
        hundreds of contract-days as failed, which is what happened when 6 workers
        got the IP blocked. Retry with growing backoff, and only give up after
        several attempts.
        """
        last_error = ""
        for attempt in range(RETRY_ATTEMPTS):
            if attempt:
                time.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
            status, rows, error = self._fetch_once(contract, win_start, win_end)
            if status != "error":
                self._net_errors = 0
                return status, rows, error
            last_error = error
            if not _is_transient(error):
                return "error", [], error       # a real API error — do not retry

        # Repeated transient failures usually mean the IP is being throttled.
        # Pause rather than racing through the remaining targets marking failures.
        self._net_errors = getattr(self, "_net_errors", 0) + 1
        if self._net_errors >= NET_ERRORS_BEFORE_PAUSE:
            print(f"\n  [network] {self._net_errors} contract-days failed in a row — "
                  f"pausing {NETWORK_PAUSE_SECONDS // 60} min to let the block clear.")
            time.sleep(NETWORK_PAUSE_SECONDS)
            self._net_errors = 0
        return "error", [], last_error

    def _fetch_once(self, contract: Contract, win_start: datetime,
                    win_end: datetime) -> tuple[str, list, str]:
        """One raw request for a time range."""
        self.throttle.acquire()
        try:
            resp = self.client.get_historical_data_v2(
                interval=self.interval,
                from_date=_iso(win_start),
                to_date=_iso(win_end),
                stock_code=contract.stock_code,
                exchange_code=contract.exchange_code,
                product_type="options",
                expiry_date=_expiry_iso(contract.expiry_date),
                right=contract.right,
                strike_price=str(contract.strike_price),
            )
        except Exception as exc:
            return "error", [], f"{type(exc).__name__}: {exc}"

        if resp.get("Status") != 200:
            error = str(resp.get("Error") or f"Status {resp.get('Status')}")
            low = error.lower()
            if any(marker in low for marker in RATE_LIMIT_MARKERS):
                raise RateLimitHit(error)
            if "no data" in low or "not found" in low:
                return "ok", [], ""
            return "error", [], error

        return "ok", (resp.get("Success") or []), ""

    def fetch_contract(self, contract: Contract) -> tuple[str, list, str]:
        """Fetch one contract-day. Returns (status, rows, error).

        Adaptive mode (default) matters at 1-second, where a fixed split costs 23
        calls per contract-day regardless of liquidity. Options are sparse — only
        seconds that traded produce candles — so most OTM strikes fit a whole day
        in ONE call. So: probe the full day first; only if the response comes back
        capped (truncated at 1000) do we pay for the fixed split.

        Deliberately not recursive bisection: a liquid ATM contract has ~20k
        candles/day, and halving repeatedly costs ~63 calls. Probe-then-split
        is 1 call best case, 1+N worst case.
        """
        base = pd.Timestamp(contract.trade_date).to_pydatetime()
        open_dt = base.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0)
        close_dt = base.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0)

        if self.args.adaptive and len(self._windows(contract.trade_date)) > 1:
            status, rows, error = self._fetch_range(contract, open_dt, close_dt)
            if status == "error":
                return "error", rows, error
            if len(rows) < MAX_CANDLES_PER_REQUEST:
                # Not truncated — this is the whole day, for one call.
                return ("ok" if rows else "empty"), rows, ""
            # Truncated: fall through to the fixed split for full coverage.

        collected: list = []
        for win_start, win_end in self._windows(contract.trade_date):
            status, rows, error = self._fetch_range(contract, win_start, win_end)
            if status == "error":
                return "error", collected, error
            collected.extend(rows)

        if not collected:
            return "empty", [], ""
        return "ok", collected, ""

    # ---------- output ----------

    def normalise(self, raw_rows: list, contract: Contract) -> list[dict]:
        out = []
        for row in raw_rows:
            ts = row.get("datetime") or row.get("date")
            out.append({
                "datetime": pd.to_datetime(ts, errors="coerce"),
                "expiry": pd.Timestamp(contract.expiry_date),
                "strike_price": contract.strike_price,
                "option_type": "CALL" if contract.right == "call" else "PUT",
                "open": pd.to_numeric(row.get("open"), errors="coerce"),
                "high": pd.to_numeric(row.get("high"), errors="coerce"),
                "low": pd.to_numeric(row.get("low"), errors="coerce"),
                "close": pd.to_numeric(row.get("close"), errors="coerce"),
                "volume": pd.to_numeric(row.get("volume"), errors="coerce"),
                "oi": pd.to_numeric(row.get("open_interest") or row.get("oi"),
                                    errors="coerce"),
                "date": contract.trade_date,
                "stock_code": contract.stock_code,
            })
        return out

    def _commit_pending(self) -> None:
        """Record buffered contract-days as done — only ever called after a
        successful parquet write, so the manifest can never claim data that
        is not on disk.

        A TRAILING run of 'empty' results is held back rather than committed: we
        cannot yet tell an untraded strike from the start of a quota wall. Once a
        non-empty result arrives the stream is proven healthy and they commit on
        the next checkpoint; if the circuit breaker trips first they are dropped
        and retried instead.
        """
        cut = len(self.pending)
        while cut > 0 and self.pending[cut - 1][1] == "empty":
            cut -= 1

        for key, status, rows, error in self.pending[:cut]:
            self.manifest.record(key, status, rows=rows, error=error)
        self.pending = self.pending[cut:]
        self.manifest.save()

    def flush(self) -> None:
        """Write buffered rows into year partitions, merging with what's there.

        Order matters: parquet first, manifest second. See _commit_pending.
        """
        if not self.buffer:
            self._commit_pending()   # 'empty'/error entries carry no rows
            return

        df = pd.DataFrame(self.buffer)
        df = df.dropna(subset=["datetime"])
        if df.empty:
            self.buffer.clear()
            self._commit_pending()
            return

        df["year"] = df["datetime"].dt.year
        for year, chunk in df.groupby("year"):
            out_dir = DATA_DIR / self.stock_code / str(year)
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"ohlcv_{self.interval}.parquet"

            chunk = chunk.drop(columns=["year"])
            if path.exists():
                existing = pd.read_parquet(path)
                chunk = pd.concat([existing, chunk], ignore_index=True)

            chunk = chunk.drop_duplicates(
                subset=["datetime", "expiry", "strike_price", "option_type"],
                keep="last",
            ).sort_values(["datetime", "expiry", "strike_price", "option_type"])
            chunk.to_parquet(path, index=False, compression="snappy")
            print(f"    [flush] {_display(path)} -> {len(chunk):,} rows")

        self.rows_written += len(df)
        self.buffer.clear()
        self._commit_pending()

    # ---------- run ----------

    def run(self) -> int:
        targets = self.build_targets()
        pending = [c for c in targets if not self.manifest.done(c.key(self.interval))]

        calls_per_contract = len(self._windows(datetime.now()))
        est_calls = len(pending) * calls_per_contract

        print("=" * 70)
        print(f"Breeze download — {self.stock_code} {self.exchange_code} {self.interval}")
        print("=" * 70)
        print(f"  Date range        : {self.args.start} to {self.args.end}")
        print(f"  Expiries per day  : {self.args.expiries}   Strikes: ATM +/- {self.args.strikes}")
        print(f"  Contract-days     : {len(targets):,} total, {len(pending):,} pending")
        print(f"  Calls per contract: {calls_per_contract}")
        print(f"  Estimated calls   : {est_calls:,}")
        print(f"  Budget left today : {self.throttle.remaining():,}")
        if est_calls:
            print(f"  Days of downloading at 4950/day: ~{est_calls / 4950:.1f}")

        if self.args.dry_run:
            print("\n  DRY RUN — no calls made.")
            return 0
        if not pending:
            print("\n  Nothing pending. Done.")
            return 0

        print(f"  Workers           : {self.args.workers}")
        print()
        try:
            self._run_parallel(pending)
        except RateLimitHit as exc:
            self.hit_wall_at = self.throttle.used
            print("\n" + "!" * 70)
            print("  SERVER REFUSED THE CALL — this is ICICI's real limit.")
            print(f"  Message      : {exc}")
            print(f"  Calls made today before refusal: {self.throttle.used:,}")
            print(f"  Contract-days completed this run: {self.contracts_done:,}")
            print("!" * 70)
        except SuspiciousEmptyRun as exc:
            dropped = sum(1 for p in self.pending if p[1] == "empty")
            self.pending = [p for p in self.pending if p[1] != "empty"]
            print("\n" + "!" * 70)
            print("  STOPPED — long unbroken run of empty responses.")
            print(f"  {exc}")
            print(f"  Discarded {dropped} unconfirmed 'empty' entries so they are retried.")
            print(f"  Calls made today: {self.throttle.used:,}")
            print("!" * 70)
        except DailyBudgetExhausted as exc:
            print(f"\n  {exc}")
        except KeyboardInterrupt:
            print("\n  Interrupted — saving progress.")
        finally:
            self.flush()

        stats = self.manifest.stats()
        print("\n" + "=" * 70)
        print(f"  Contract-days this run : {self.contracts_done:,}")
        print(f"  Rows written this run  : {self.rows_written:,}")
        print(f"  Manifest totals        : {stats}")
        print(f"  Calls used today       : {self.throttle.used:,}")
        print("=" * 70)
        return 0

    def _run_parallel(self, pending: list) -> None:
        """Fetch concurrently; apply results serially.

        Only the network call is parallel. Buffer/manifest mutation stays on this
        thread, so the write-parquet-then-commit-manifest ordering — and the
        crash-safety it buys — is unchanged by concurrency.
        """
        workers = max(1, int(self.args.workers))
        if workers == 1:
            for i, contract in enumerate(pending, 1):
                self._apply(contract, *self.fetch_contract(contract))
                self._checkpoint(i, len(pending))
            return

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit in bounded waves so a stop doesn't leave thousands of
            # futures queued, and so the manifest checkpoints regularly.
            wave = workers * 8
            for start in range(0, len(pending), wave):
                chunk = pending[start:start + wave]
                futures = {pool.submit(self.fetch_contract, c): c for c in chunk}
                for future in as_completed(futures):
                    contract = futures[future]
                    self._apply(contract, *future.result())
                    done += 1
                    self._checkpoint(done, len(pending))

    def _apply(self, contract: Contract, status: str, raw: list, error: str) -> None:
        self.pending.append((contract.key(self.interval), status, len(raw), error))

        if status == "empty":
            self.consecutive_empty += 1
            if self.consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                raise SuspiciousEmptyRun(
                    f"{self.consecutive_empty} consecutive empty responses "
                    f"(last: {contract.trade_date} {contract.strike_price}"
                    f"{contract.right[0].upper()} exp {contract.expiry_date})"
                )
        else:
            self.consecutive_empty = 0

        if status == "ok":
            self.buffer.extend(self.normalise(raw, contract))
        elif status == "error":
            print(f"  [err ] {contract.trade_date} {contract.strike_price}"
                  f"{contract.right[0].upper()} exp {contract.expiry_date}: {error}")

        self.contracts_done += 1

    def _checkpoint(self, i: int, total: int) -> None:
        if i % 25 == 0 or i == total:
            self.flush()
            elapsed = time.time() - self.started
            rate = i / elapsed * 60 if elapsed else 0
            calls_min = self.throttle.used / elapsed * 60 if elapsed else 0
            print(f"  {i:,}/{total:,} contract-days  "
                  f"buffer={len(self.buffer):,} rows  "
                  f"used={self.throttle.used:,}  "
                  f"{rate:.0f} cd/min  ~{calls_min:.0f} calls/min")
        if len(self.buffer) >= self.args.flush_every:
            self.flush()

def main() -> int:
    ap = argparse.ArgumentParser(description="Breeze bulk options downloader")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--interval", default="1minute",
                    choices=["1second", "1minute", "5minute", "30minute", "1day"])
    ap.add_argument("--expiries", type=int, default=2,
                    help="expiries per trade date (1=front week, 2=front+next)")
    ap.add_argument("--strikes", type=int, default=10, help="strikes each side of ATM")
    ap.add_argument("--stock-code", default="NIFTY", dest="stock_code")
    ap.add_argument("--exchange-code", default="NFO", dest="exchange_code")
    ap.add_argument("--flush-every", type=int, default=200_000,
                    help="buffered rows before writing to parquet")
    ap.add_argument("--adaptive", action="store_true", default=True,
                    help="request the full day and bisect only when capped (default)")
    ap.add_argument("--no-adaptive", dest="adaptive", action="store_false",
                    help="always split into fixed windows")
    ap.add_argument("--workers", type=int, default=6,
                    help="concurrent requests (default 6). Sequential fetching "
                         "only reaches ~46 calls/min; the API allows 100/min")
    ap.add_argument("--per-minute", type=int, default=None,
                    help="override the client-side calls/minute ceiling")
    ap.add_argument("--ignore-daily-cap", action="store_true",
                    help="do not self-limit at 5000/day — keep going until the "
                         "SERVER refuses, so we learn the real enforced limit")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan and call cost without making any calls")
    args = ap.parse_args()

    try:
        return Downloader(args).run()
    except RuntimeError as exc:
        print(f"\n{exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
