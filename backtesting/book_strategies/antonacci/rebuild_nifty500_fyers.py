"""
rebuild_nifty500_fyers.py
Rebuild the Nifty 500 daily OHLCV dataset from the Fyers History API.

Why: the existing "Nifty 500 Daily Data" parquets are NOT corporate-action
adjusted (94 suspected split/bonus/demerger events across 84 of 500 symbols),
so every 12-month return computed across such an event is wrong. Fyers History
back-adjusts (verified 5/6 on known events), so we re-source from it.

Output: Nifty 500 Daily Fyers/{SYMBOL}.parquet   (same schema as the old set)

Notes
  - Fyers caps a daily-resolution request at 366 days, so we walk year windows.
  - Walks newest -> oldest and stops after STOP_AFTER_EMPTY consecutive empty
    years, so recently-listed names don't burn 20 pointless requests each.
  - The VPS rotates the shared Fyers token; on an auth error we re-copy the
    token from the VPS and retry rather than dying mid-run.
  - Resume-safe: already-written symbols are skipped unless --force.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import json
import subprocess
import time
from pathlib import Path

import pandas as pd
from fyers_apiv3 import fyersModel

ROOT = Path(r"G:\fyers_data_pipeline")
sys.path.insert(0, str(ROOT))
from deployment.nifty500_symbols import NIFTY500  # noqa: E402

OUT_DIR    = ROOT / "Nifty 500 Daily Fyers"
TOKEN_PATH = ROOT / "config" / "access_token.txt"
APP_ID     = "W09OMXQB8J-100"

VPS_TOKEN  = ("Administrator@103.49.131.58:"
              "C:/trading/fyers_data_pipeline/config/access_token.txt")

START_YEAR        = 2005
END_YEAR          = 2026
STOP_AFTER_EMPTY  = 2      # consecutive empty years before giving up going back
SLEEP             = 0.12   # between requests (~8/sec, under the Fyers limit)
MAX_RETRY         = 4

_fy = None


def _refresh_token_from_vps() -> bool:
    """Copy the current token from the VPS. The VPS is the only place a token
    is ever generated — generating one locally would kill the VPS live feed."""
    try:
        subprocess.run(["scp", "-q", VPS_TOKEN, str(TOKEN_PATH)],
                       check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"    ! token refresh from VPS failed: {e}")
        return False


def _connect():
    global _fy
    tok = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))["token"]
    _fy = fyersModel.FyersModel(client_id=f"{APP_ID}:{tok}", is_async=False,
                                token=tok, log_path="")
    return _fy


def fetch_window(symbol: str, year: int):
    """One year of daily candles. Returns (DataFrame|None, status)."""
    req = {"symbol": f"NSE:{symbol}-EQ", "resolution": "D", "date_format": "1",
           "range_from": f"{year}-01-01", "range_to": f"{year}-12-31",
           "cont_flag": "1"}
    for attempt in range(MAX_RETRY):
        try:
            r = _fy.history(req)
        except Exception as e:
            time.sleep(1.5 * (attempt + 1))
            if attempt == MAX_RETRY - 1:
                return None, f"exception: {e}"
            continue

        if r.get("s") == "ok":
            candles = r.get("candles") or []
            if not candles:
                return None, "empty"
            d = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
            d["date"] = (pd.to_datetime(d["ts"], unit="s", utc=True)
                           .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize())
            return d.drop(columns=["ts"]).set_index("date"), "ok"

        # Fyers returns s="no_data" (not an error) for years before the listing date
        if r.get("s") == "no_data":
            return None, "empty"

        msg = str(r.get("message", r))
        if "authenticate" in msg.lower() or r.get("code") == -16:
            print("    token expired mid-run — refreshing from VPS...")
            if _refresh_token_from_vps():
                _connect()
                time.sleep(0.5)
                continue
            return None, "auth-failed"
        if "rate" in msg.lower() or "limit" in msg.lower():
            time.sleep(2.0 * (attempt + 1))
            continue
        return None, msg[:60]
    return None, "retries-exhausted"


def rebuild_symbol(symbol: str):
    frames, empty_streak, calls = [], 0, 0
    for year in range(END_YEAR, START_YEAR - 1, -1):
        d, status = fetch_window(symbol, year)
        calls += 1
        time.sleep(SLEEP)
        if status == "ok":
            frames.append(d)
            empty_streak = 0
        elif status == "empty":
            empty_streak += 1
            if empty_streak >= STOP_AFTER_EMPTY and frames:
                break          # listed later than this — stop walking back
            if empty_streak >= 4 and not frames:
                break          # nothing anywhere; likely a dead/renamed symbol
        else:
            return None, status, calls

    if not frames:
        return None, "no-data", calls

    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df["symbol"] = f"NSE:{symbol}-EQ"
    df["volume"] = df["volume"].astype("int64")
    return df[["open", "high", "low", "close", "volume", "symbol"]], "ok", calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download symbols already saved")
    ap.add_argument("--limit", type=int, default=0, help="only process first N symbols (testing)")
    args = ap.parse_args()

    OUT_DIR.mkdir(exist_ok=True)
    _connect()

    symbols = NIFTY500[:args.limit] if args.limit else NIFTY500
    todo = [s for s in symbols if args.force or not (OUT_DIR / f"{s}.parquet").exists()]
    print(f"Rebuilding {len(todo)} of {len(symbols)} symbols -> {OUT_DIR}")
    print(f"Range {START_YEAR}-{END_YEAR}, 366-day windows, ~{SLEEP}s between calls\n")

    ok = failed = 0
    total_calls = 0
    failures = []
    t0 = time.time()

    for i, sym in enumerate(todo, 1):
        df, status, calls = rebuild_symbol(sym)
        total_calls += calls
        if df is None:
            failed += 1
            failures.append((sym, status))
            print(f"  [{i:>3}/{len(todo)}] {sym:<14} FAILED  ({status})")
        else:
            df.to_parquet(OUT_DIR / f"{sym}.parquet")
            ok += 1
            print(f"  [{i:>3}/{len(todo)}] {sym:<14} {len(df):>5} bars  "
                  f"{df.index[0].date()} -> {df.index[-1].date()}  ({calls} calls)")

        if i % 25 == 0:
            el = time.time() - t0
            rate = i / el * 60
            print(f"      ... {i}/{len(todo)} done, {el/60:.1f} min elapsed, "
                  f"{rate:.1f} sym/min, ETA {(len(todo)-i)/rate:.0f} min\n")

    print(f"\n{'='*60}")
    print(f"Done in {(time.time()-t0)/60:.1f} min | ok={ok}  failed={failed}  "
          f"api_calls={total_calls}")
    if failures:
        print("\nFailures:")
        for s, why in failures:
            print(f"  {s:<14} {why}")


if __name__ == "__main__":
    main()
