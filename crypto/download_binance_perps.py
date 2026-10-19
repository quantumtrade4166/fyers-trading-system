import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Binance USDT-M perpetuals: 1h klines (with taker-buy volume) + funding history
===============================================================================
Source: https://data.binance.vision/data/futures/um  (free, no key)
        monthly zips for closed months, daily zips for the current month.

Universe: crypto/universe/universe_top100.csv (build_universe.py)

Output (git-ignored), one folder per contract:
  data/CRYPTO_PERPS/{PERP}/klines_1h.parquet
      datetime (UTC, naive, bar open), open, high, low, close,
      volume (base), quote_volume (USDT), trades, taker_buy_volume, taker_buy_quote
  data/CRYPTO_PERPS/{PERP}/funding.parquet
      datetime (UTC), funding_rate (per settlement)

Re-runnable: resumes from the last stored month. A missing zip (404) is fine
(month before listing / delisted); any other error is raised, never skipped.

Run: .venv\\Scripts\\python.exe crypto\\download_binance_perps.py [--only BTCUSDT ETHUSDT] [--workers 4]
"""

import argparse
import io
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parents[1]
OUT = ROOT / "data" / "CRYPTO_PERPS"
UNIVERSE = ROOT / "crypto" / "universe" / "universe_top100.csv"
BASE = "https://data.binance.vision/data/futures/um"

KCOLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote", "ignore"]


def _fetch(url: str, retries: int = 4) -> bytes | None:
    err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fyers-pipeline/perps"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:
            err = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {err}")


def _read_zip_csv(blob: bytes, cols: list[str] | None) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0]).decode()
    first = raw.split("\n", 1)[0]
    has_header = not first[:1].isdigit()
    df = pd.read_csv(io.StringIO(raw), header=0 if has_header else None)
    if cols is not None:
        df.columns = cols
    return df


def _months(start: date, end_excl: date):
    d = date(start.year, start.month, 1)
    while d < end_excl:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def _klines(perp: str, start: date, today: date) -> pd.DataFrame:
    q = urllib.parse.quote(perp)
    this_month = date(today.year, today.month, 1)
    parts = []
    last_found = None
    for m in _months(start, this_month):
        blob = _fetch(f"{BASE}/monthly/klines/{q}/1h/{q}-1h-{m:%Y-%m}.zip")
        if blob:
            parts.append(_read_zip_csv(blob, KCOLS))
            last_found = m
    prev_month = date(this_month.year - (this_month.month == 1), (this_month.month - 2) % 12 + 1, 1)
    d = max(this_month, start)
    if start < this_month and last_found != prev_month:
        d = today                                   # delisted — no daily files to look for
    while d < today:
        blob = _fetch(f"{BASE}/daily/klines/{q}/1h/{q}-1h-{d:%Y-%m-%d}.zip")
        if blob:
            parts.append(_read_zip_csv(blob, KCOLS))
        d += timedelta(days=1)
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    ot = pd.to_numeric(df["open_time"])
    unit = "us" if ot.max() > 1e14 else "ms"          # Binance moved to µs in 2025
    df["datetime"] = pd.to_datetime(ot, unit=unit)
    keep = ["datetime", "open", "high", "low", "close", "volume", "quote_volume",
            "trades", "taker_buy_volume", "taker_buy_quote"]
    df = df[keep].astype({c: "float64" for c in keep[1:]})
    return df


def _funding(perp: str, start: date, today: date) -> pd.DataFrame:
    q = urllib.parse.quote(perp)
    parts = []
    for m in _months(start, date(today.year, today.month, 1)):
        blob = _fetch(f"{BASE}/monthly/fundingRate/{q}/{q}-fundingRate-{m:%Y-%m}.zip")
        if blob:
            parts.append(_read_zip_csv(blob, None))
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    t = pd.to_numeric(df["calc_time"])
    df["datetime"] = pd.to_datetime(t, unit="us" if t.max() > 1e14 else "ms").dt.floor("min")
    df["funding_rate"] = df["last_funding_rate"].astype("float64")
    return df[["datetime", "funding_rate"]]


def _merge_save(path: Path, new: pd.DataFrame) -> int:
    if new.empty:
        return 0
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    new = new.drop_duplicates("datetime", keep="last").sort_values("datetime")
    tmp = path.with_suffix(".tmp")
    new.to_parquet(tmp, index=False)
    tmp.replace(path)
    return len(new)


def _resume_from(path: Path, onboard: date) -> date:
    if not path.exists():
        return onboard
    last = pd.read_parquet(path, columns=["datetime"])["datetime"].max().date()
    return date(last.year, last.month, 1)          # refetch the last month


S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
ALL_SYMBOLS = ROOT / "crypto" / "universe" / "all_um_symbols.txt"
# index / basket contracts and stablecoins — not single coins
NOT_COINS = {"BTCDOMUSDT", "DEFIUSDT", "FOOTBALLUSDT", "BLUEBIRDUSDT", "USDCUSDT",
             "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "USDPUSDT", "USDEUSDT", "USD1USDT",
             "XAUUSDT", "XAGUSDT", "PAXGUSDT", "XAUTUSDT", "EURUSDT"}


def _first_listed_month(perp: str) -> date | None:
    """First monthly kline zip in the public archive (None = no monthly files)."""
    import re
    q = urllib.parse.quote(f"data/futures/um/monthly/klines/{perp}/1h/")
    blob = _fetch(f"{S3}?prefix={q}&max-keys=3")
    m = re.search(rb"-1h-(\d{4})-(\d{2})\.zip<", blob or b"")
    return date(int(m.group(1)), int(m.group(2)), 1) if m else None


def all_perps() -> list[str]:
    syms = ALL_SYMBOLS.read_text(encoding="utf-8").split()
    return [s for s in syms if s.endswith("USDT") and s not in NOT_COINS]


def do_one(perp: str, onboard: date, today: date) -> str:
    d = OUT / perp
    d.mkdir(parents=True, exist_ok=True)
    kp, fp = d / "klines_1h.parquet", d / "funding.parquet"
    nk = _merge_save(kp, _klines(perp, _resume_from(kp, onboard), today))
    nf = _merge_save(fp, _funding(perp, _resume_from(fp, onboard), today))
    return f"{perp:<16} klines {nk:>6}  funding {nf:>5}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--all", action="store_true",
                    help="every USDT perp in the archive, incl. delisted (all_um_symbols.txt)")
    args = ap.parse_args()

    today = date.today()
    if args.all:
        todo = [(p, None) for p in all_perps()]
    else:
        uni = pd.read_csv(UNIVERSE, parse_dates=["onboard"])
        todo = [(r.perp, r.onboard.date()) for r in uni.itertuples()]
    if args.only:
        todo = [t for t in todo if t[0] in args.only]
    t0 = time.time()
    print(f"{len(todo)} contracts, workers={args.workers}", flush=True)

    def job(perp, onboard):
        if onboard is None:
            onboard = _first_listed_month(perp)
            if onboard is None:
                return f"{perp:<16} no monthly files - skipped"
        return do_one(perp, onboard, today)

    with ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(job, p, ob): p for p, ob in todo}
        failed = []
        for i, f in enumerate(futs, 1):
            try:
                print(f"[{i:>3}/{len(futs)}] {f.result()}", flush=True)
            except Exception as e:
                failed.append(futs[f])
                print(f"[{i:>3}/{len(futs)}] {futs[f]} FAILED: {e}", flush=True)
    print(f"done in {time.time() - t0:.0f}s; failed: {failed or 'none'}", flush=True)


if __name__ == "__main__":
    main()
