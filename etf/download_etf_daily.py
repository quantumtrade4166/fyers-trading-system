# ============================================================
# etf/download_etf_daily.py
# Downloads full daily OHLCV history for NSE ETFs from Fyers.
#
# Fyers serves daily bars back to 2002 (NIFTYBEES starts 2002-01-08),
# but the history endpoint accepts a maximum of ~1 year per call at
# "D" resolution, so each symbol is pulled year by year.
#
# Output: "ETF data/{SYMBOL}.parquet"  (one file per ETF)
#   index  : date (datetime64)
#   columns: open, high, low, close, volume, symbol
#   -> same schema as "Nifty 500 Daily Data/"
#
# NOTE: must be run from an IP Fyers does not flag as a VPN. The local
# workstation is blocked (Tailscale) — run this on the VPS.
#
#   python etf/download_etf_daily.py --core
#   python etf/download_etf_daily.py --all --sleep 0.4
#   python etf/download_etf_daily.py --symbols NSE:NIFTYBEES-EQ
# ============================================================

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from fyers_apiv3 import fyersModel

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.append(str(Path(__file__).parent.parent))

from etf.etf_universe import CORE_ETFS, fetch_all_etfs, short_name  # noqa: E402

APP_ID = "W09OMXQB8J-100"
BASE_DIR = Path(__file__).parent.parent
DEFAULT_OUT = BASE_DIR / "ETF data"
DEFAULT_TOKEN = BASE_DIR / "config" / "access_token.txt"

MIN_YEAR = 2001          # nothing on NSE ETFs predates this
EMPTY_YEARS_STOP = 2     # stop walking back after this many blank years in a row
MAX_RETRIES = 3

COLUMNS = ["open", "high", "low", "close", "volume", "symbol"]


# ── Fyers client ─────────────────────────────────────────────

def make_client(token_path: Path):
    payload = json.loads(token_path.read_text())
    token = payload["token"] if isinstance(payload, dict) else payload
    client = fyersModel.FyersModel(
        client_id=APP_ID, token=token, is_async=False,
        log_path=str(BASE_DIR / "logs"),
    )
    profile = client.get_profile()
    if profile.get("s") != "ok":
        raise SystemExit(f"Fyers auth failed: {profile}")
    print(f"Fyers auth OK (token dated {payload.get('date', '?')})")
    return client


# ── One year of daily bars ───────────────────────────────────

def fetch_year(client, symbol: str, from_d: date, to_d: date, sleep: float):
    """
    Returns (DataFrame|None, status) where status is 'ok' | 'empty' | 'error'.
    'empty' means the symbol simply did not trade in this window (pre-listing);
    'error' means the call failed after retries.
    """
    req = {
        "symbol": symbol, "resolution": "D", "date_format": "1",
        "range_from": str(from_d), "range_to": str(to_d), "cont_flag": "1",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.history(data=req)
        except Exception as exc:
            print(f"    {from_d.year}: exception {exc}")
            time.sleep(2 ** attempt)
            continue

        if resp.get("s") == "ok":
            candles = resp.get("candles") or []
            if not candles:
                return None, "empty"
            df = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
            df["date"] = pd.to_datetime(df["epoch"], unit="s").dt.normalize()
            df["symbol"] = symbol
            return df.drop(columns=["epoch"]).set_index("date")[COLUMNS], "ok"

        msg = str(resp.get("message", ""))
        # Pre-listing windows come back as a plain error with no message.
        if not msg or "no data" in msg.lower():
            return None, "empty"
        # Rate limited — back off hard, then retry.
        if "limit" in msg.lower() or resp.get("code") == 429:
            wait = 30 * attempt
            print(f"    rate limited, sleeping {wait}s")
            time.sleep(wait)
            continue
        print(f"    {from_d.year}: {msg[:70]}")
        time.sleep(sleep * 2)

    return None, "error"


# ── One symbol, full history ─────────────────────────────────

def download_symbol(client, symbol: str, out_dir: Path, sleep: float,
                    force: bool = False) -> dict:
    name = short_name(symbol)
    path = out_dir / f"{name}.parquet"
    today = date.today()

    existing = None
    if path.exists() and not force:
        existing = pd.read_parquet(path)
        last = existing.index.max().date()
        if last >= today - timedelta(days=1):
            return {"symbol": symbol, "status": "up_to_date", "bars": len(existing),
                    "first": str(existing.index.min().date()), "last": str(last)}
        start_year = last.year
    else:
        start_year = None  # full history walk

    frames = []

    if start_year is not None:
        # Incremental: just the years touched since the last saved bar.
        for year in range(start_year, today.year + 1):
            df, _ = fetch_year(client, symbol, date(year, 1, 1),
                               min(date(year, 12, 31), today), sleep)
            if df is not None:
                frames.append(df)
            time.sleep(sleep)
    else:
        # Full history: walk backwards until the symbol stops existing.
        empty_streak = 0
        for year in range(today.year, MIN_YEAR - 1, -1):
            df, status = fetch_year(client, symbol, date(year, 1, 1),
                                    min(date(year, 12, 31), today), sleep)
            time.sleep(sleep)
            if status == "ok":
                frames.append(df)
                empty_streak = 0
            else:
                empty_streak += 1
                if empty_streak >= EMPTY_YEARS_STOP and frames:
                    break
                if empty_streak >= EMPTY_YEARS_STOP + 1 and not frames:
                    break  # symbol has no data at all

    if not frames and existing is None:
        return {"symbol": symbol, "status": "no_data", "bars": 0}

    if existing is not None:
        frames.append(existing)

    full = (pd.concat(frames)
              .reset_index()
              .drop_duplicates(subset=["date"], keep="first")
              .sort_values("date")
              .set_index("date")[COLUMNS])

    out_dir.mkdir(parents=True, exist_ok=True)
    full.to_parquet(path, compression="snappy")

    return {"symbol": symbol, "status": "ok", "bars": len(full),
            "first": str(full.index.min().date()), "last": str(full.index.max().date())}


# ── Manifest ─────────────────────────────────────────────────

def build_manifest(out_dir: Path, names: dict) -> pd.DataFrame:
    """
    Summarise everything on disk: coverage, recent liquidity, and the
    largest one-day move (a crude corporate-action / split detector).
    """
    rows = []
    for path in sorted(out_dir.glob("*.parquet")):
        df = pd.read_parquet(path)
        if df.empty:
            continue
        sym = df["symbol"].iloc[0]
        close = df["close"]
        turnover = (close * df["volume"]).tail(60)
        ret = close.pct_change().abs()
        rows.append({
            "symbol": sym,
            "name": names.get(sym, ""),
            "first": df.index.min().date(),
            "last": df.index.max().date(),
            "bars": len(df),
            "years": round((df.index.max() - df.index.min()).days / 365.25, 1),
            "median_turnover_60d": round(float(turnover.median()), 0) if len(turnover) else 0.0,
            "max_1d_move": round(float(ret.max()), 4) if len(ret.dropna()) else 0.0,
            "max_1d_move_on": str(ret.idxmax().date()) if len(ret.dropna()) else "",
        })

    man = pd.DataFrame(rows).sort_values("median_turnover_60d", ascending=False)
    man.to_csv(out_dir / "_manifest.csv", index=False)
    return man


# ── CLI ──────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Download daily ETF history from Fyers")
    ap.add_argument("--core", action="store_true", help="core liquid ETF list (default)")
    ap.add_argument("--all", action="store_true", help="every exchange-traded NSE ETF")
    ap.add_argument("--symbols", nargs="+", help="explicit symbol list")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--token", default=str(DEFAULT_TOKEN))
    ap.add_argument("--sleep", type=float, default=1.2,
                    help="seconds between API calls (1.2 during market hours, 0.4 after close)")
    ap.add_argument("--force", action="store_true", help="re-download full history")
    ap.add_argument("--manifest-only", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    names = {}
    try:
        names = dict(fetch_all_etfs(cache=out_dir / "_symbol_master.csv"))
    except Exception as exc:
        print(f"(symbol master unavailable: {exc})")

    if args.manifest_only:
        man = build_manifest(out_dir, names)
        print(man.to_string(index=False))
        return

    if args.symbols:
        symbols = args.symbols
    elif args.all:
        symbols = [t for t, _ in sorted(names.items())] or CORE_ETFS
    else:
        symbols = CORE_ETFS

    client = make_client(Path(args.token))
    print(f"Downloading {len(symbols)} ETFs -> {out_dir}  (sleep={args.sleep}s)\n")

    started = time.time()
    results = []
    for i, sym in enumerate(symbols, 1):
        try:
            res = download_symbol(client, sym, out_dir, args.sleep, args.force)
        except Exception as exc:
            res = {"symbol": sym, "status": f"exception: {exc}", "bars": 0}
        results.append(res)
        print(f"[{i:>3}/{len(symbols)}] {short_name(sym):<14} "
              f"{res['status']:<11} {res.get('bars', 0):>6} bars  "
              f"{res.get('first', '')} -> {res.get('last', '')}")

    ok = [r for r in results if r["status"] in ("ok", "up_to_date")]
    print(f"\nDone in {(time.time() - started) / 60:.1f} min — "
          f"{len(ok)}/{len(symbols)} succeeded, {sum(r.get('bars', 0) for r in ok):,} bars")

    bad = [r for r in results if r["status"] not in ("ok", "up_to_date")]
    if bad:
        print("Failed:", ", ".join(f"{short_name(r['symbol'])}({r['status']})" for r in bad))

    man = build_manifest(out_dir, names)
    print(f"\nManifest -> {out_dir / '_manifest.csv'}  ({len(man)} symbols)")


if __name__ == "__main__":
    main()
