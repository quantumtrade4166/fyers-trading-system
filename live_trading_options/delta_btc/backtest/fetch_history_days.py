"""
backtest/fetch_history_days.py — pull Delta's HISTORY for specific settlement days.
==================================================================================

Same API calls and the same output format as `Bitcoin options data/
download_delta_btc_options.py` (1-minute MARK candles + 1-minute TRADE candles over
the last 24h of each contract's life, plus the .DEXBTUSD index), so a backtest run
on these files measures exactly what it measures on the main dataset.

The one difference: the contract list. The main downloader lists every expired
BTC option from /products, which takes ~15 min and is cached for a day. Here the
symbols come from OUR chain archive, which snapshotted the whole chain every
minute — every strike that was ever listed for that expiry.

Output goes to a SEPARATE folder so the main dataset is never touched:
    Bitcoin options data/validation/raw/<day>.parquet
    Bitcoin options data/validation/spot/<day>.parquet

    .venv\Scripts\python.exe live_trading_options\delta_btc\backtest\fetch_history_days.py 2026-09-16 2026-09-17
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import glob
import time
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests
import pandas as pd

BASE = "https://api.india.delta.exchange/v2"
OUT = ROOT.parents[1] / "Bitcoin options data" / "validation"
IST = dt.timedelta(hours=5, minutes=30)
S = requests.Session()


def get(path, params, tries=8):
    for i in range(tries):
        try:
            r = S.get(BASE + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + 3 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            time.sleep(2 + 3 * i)
    raise RuntimeError(f"failed {path} {params}")


def candles(symbol, start, end):
    return get("/history/candles", {"symbol": symbol, "resolution": "1m",
                                    "start": start, "end": end}).get("result") or []


def symbols_for(day: dt.date) -> pd.DataFrame:
    code = f"{day.day:02d}{day.month:02d}{day.year % 100:02d}"
    rows = []
    for d in (day - dt.timedelta(days=1), day):
        for f in glob.glob(str(ROOT / "data" / "chain_archive" / d.isoformat() / "chunk_*.parquet")):
            try:
                x = pd.read_parquet(f, columns=["symbol", "opt_type", "strike", "contract_value"])
            except Exception:
                continue
            rows.append(x[x["symbol"].astype(str).str.endswith(code)])
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows).drop_duplicates("symbol")


def one(row, start, end):
    mk = {c["time"]: c for c in candles("MARK:" + row["symbol"], start, end)}
    tr = {c["time"]: c for c in candles(row["symbol"], start, end)}
    out = []
    for t in sorted(set(mk) | set(tr)):
        m, x = mk.get(t, {}), tr.get(t, {})
        out.append({"time": t, "symbol": row["symbol"], "opt_type": row["opt_type"],
                    "strike": float(row["strike"]),
                    "contract_value": float(row.get("contract_value") or 0.001),
                    "mark_open": m.get("open"), "mark_high": m.get("high"),
                    "mark_low": m.get("low"), "mark": m.get("close"),
                    "open": x.get("open"), "high": x.get("high"), "low": x.get("low"),
                    "close": x.get("close"), "volume": x.get("volume")})
    return out


def main():
    (OUT / "raw").mkdir(parents=True, exist_ok=True)
    (OUT / "spot").mkdir(parents=True, exist_ok=True)
    for arg in sys.argv[1:]:
        day = dt.date.fromisoformat(arg)
        syms = symbols_for(day)
        if syms.empty:
            print(f"  {day}: no symbols in the chain archive — skipped")
            continue
        # settlement 17:30 IST = 12:00 UTC; the last 24h of the contract's life
        end = int(dt.datetime(day.year, day.month, day.day, 12, 0,
                              tzinfo=dt.timezone.utc).timestamp())
        start = end - 86400
        t0 = time.time()
        with ThreadPoolExecutor(6) as ex:
            parts = list(ex.map(lambda r: one(r, start, end), syms.to_dict("records")))
        df = pd.DataFrame([x for p in parts for x in p])
        if not df.empty:
            df["time_ist"] = pd.to_datetime(df["time"], unit="s") + IST
            df["expiry"] = day.strftime("%d%m%y")
        df.to_parquet(OUT / "raw" / f"{day}.parquet", index=False)
        spot = candles(".DEXBTUSD", start, end)
        if spot:
            sp = pd.DataFrame(spot)
            sp["time_ist"] = pd.to_datetime(sp["time"], unit="s") + IST
            sp.to_parquet(OUT / "spot" / f"{day}.parquet", index=False)
        with_mark = int(df["mark"].notna().sum()) if not df.empty else 0
        print(f"  {day}: {len(syms)} contracts, {len(df):,} rows ({with_mark:,} with a mark), "
              f"spot {len(spot)}  ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
