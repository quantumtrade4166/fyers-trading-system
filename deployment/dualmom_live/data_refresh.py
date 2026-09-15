"""
Daily refresh of the Nifty 500 price parquets that feed the DualMom signal.

WHY THIS IS NOT JUST "APPEND TODAY'S ROW"
    Fyers back-adjusts history for corporate actions — that is the whole reason
    this dataset was re-sourced from Fyers in the first place (the older set had
    94 suspected split/bonus events across 84 symbols, and every 12-month return
    computed across one of them was wrong).

    So when a stock splits 1:2, Fyers rewrites its ENTIRE past. A blind append
    would leave 20 years of unadjusted history sitting under one freshly-adjusted
    row, and the 12-month return for that name would be off by ~100% — enough to
    put it at the very top of a momentum ranking and buy it with real money.

    This module therefore re-fetches a short OVERLAP window on every run and
    compares it against what is stored. If the stored closes disagree, that
    symbol has been re-adjusted and its full history is refetched.

USAGE
    python -m deployment.dualmom_live.data_refresh              # incremental
    python -m deployment.dualmom_live.data_refresh --symbols RELIANCE,HFCL
    python -m deployment.dualmom_live.data_refresh --full       # rebuild all
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "Nifty 500 Daily Fyers"
TOKEN_PATH = ROOT / "config" / "access_token.txt"
APP_ID = os.getenv("FYERS_APP_ID", "W09OMXQB8J-100")
INDEX_SYMBOL = "NSE:NIFTY50-INDEX"
INDEX_FILE = "_NIFTY50_INDEX.parquet"

OVERLAP_DAYS = 45          # re-fetched every run to detect a re-adjustment
ADJUST_TOL = 0.005         # >0.5% disagreement on a stored close = re-adjusted
FIRST_YEAR = 2005
REQ_PAUSE = 0.12           # ~8 req/s, under Fyers' limit
MAX_RETRY = 3


def _connect():
    """Match deployment/dualmom_paper.py exactly.

    Two easy ways to get "Could not authenticate the user" here: the token file is
    sometimes JSON ({"token": ...}) rather than a bare string, and fyers_apiv3
    wants the token embedded in client_id as "APPID:token" as well as passed
    separately. Getting either wrong authenticates as nobody.
    """
    from fyers_apiv3 import fyersModel
    raw = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError(f"empty Fyers token at {TOKEN_PATH}")
    try:
        import json as _json
        token = _json.loads(raw)["token"]
    except Exception:
        token = raw
    return fyersModel.FyersModel(client_id=f"{APP_ID}:{token}", is_async=False,
                                 token=token, log_path="")


def _history(fy, symbol: str, frm: date, to: date) -> pd.DataFrame:
    """One daily-resolution window. Fyers caps a request at 366 days."""
    req = {"symbol": symbol, "resolution": "D", "date_format": "1",
           "range_from": frm.isoformat(), "range_to": to.isoformat(), "cont_flag": "1"}
    last = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            r = fy.history(req)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * attempt)
            continue
        if not isinstance(r, dict):
            last = f"non-dict response: {str(r)[:120]}"
        elif r.get("s") == "ok":
            candles = r.get("candles") or []
            if not candles:
                return pd.DataFrame()
            d = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
            d["date"] = pd.to_datetime(d["ts"], unit="s").dt.normalize()
            return d.drop(columns=["ts"]).set_index("date")
        else:
            last = str(r.get("message") or r)[:160]
            # an auth failure will not fix itself by retrying
            if "token" in (last or "").lower() or "auth" in (last or "").lower():
                raise RuntimeError(f"Fyers auth failed: {last}")
        time.sleep(1.0 * attempt)
    raise RuntimeError(f"history {symbol} {frm}..{to}: {last}")


def _fetch_range(fy, symbol: str, frm: date, to: date) -> pd.DataFrame:
    """Walk 366-day windows and concatenate."""
    out = []
    cur = frm
    while cur <= to:
        end = min(cur + timedelta(days=360), to)
        d = _history(fy, symbol, cur, end)
        if not d.empty:
            out.append(d)
        time.sleep(REQ_PAUSE)
        cur = end + timedelta(days=1)
    if not out:
        return pd.DataFrame()
    d = pd.concat(out)
    return d[~d.index.duplicated(keep="last")].sort_index()


def _readjusted(stored: pd.DataFrame, fresh: pd.DataFrame) -> bool:
    """Did Fyers rewrite this symbol's past? Compare the overlapping closes."""
    common = stored.index.intersection(fresh.index)
    if len(common) < 3:
        return False           # too little overlap to judge; don't refetch 20y
    a = stored.loc[common, "close"].astype(float)
    b = fresh.loc[common, "close"].astype(float)
    both = (a > 0) & (b > 0)
    if both.sum() < 3:
        return False
    rel = ((a[both] - b[both]).abs() / b[both])
    return bool((rel > ADJUST_TOL).mean() > 0.5)   # most of the window disagrees


# A stock can MOVE between series. HFCL sits in EQ in the stored history but is
# now "Invalid symbol provided" as NSE:HFCL-EQ, because NSE shifted it to the BE
# (trade-for-trade) segment — the same move Kotak's scrip master already showed us
# as HFCL-BE. Hardcoding -EQ silently stops updating such a name, and a stale
# price feeds a stale 12-month return into a live momentum ranking.
SERIES_SUFFIXES = ("-EQ", "-BE")


def _resolve_fyers_symbol(fy, symbol: str, probe_from: date, probe_to: date) -> str:
    """Return the Fyers symbol that actually works for this stock."""
    last_err = None
    for suf in SERIES_SUFFIXES:
        cand = f"NSE:{symbol}{suf}"
        try:
            _history(fy, cand, probe_from, probe_to)
            return cand
        except RuntimeError as e:
            if "auth" in str(e).lower():
                raise
            last_err = e
            continue
    raise RuntimeError(f"no working NSE series for {symbol} "
                       f"(tried {', '.join(SERIES_SUFFIXES)}): {last_err}")


def refresh_symbol(fy, symbol: str, full: bool = False) -> dict:
    """Bring one symbol's parquet up to date. Returns a small report."""
    is_index = symbol == INDEX_SYMBOL
    fyers_sym = symbol if is_index else f"NSE:{symbol}-EQ"
    path = DATA_DIR / (INDEX_FILE if is_index else f"{symbol}.parquet")
    today = date.today()

    stored = None
    if path.exists() and not full:
        try:
            stored = pd.read_parquet(path)
        except Exception:
            stored = None

    if stored is None or stored.empty:
        if not is_index:
            try:
                fyers_sym = _resolve_fyers_symbol(
                    fy, symbol, today - timedelta(days=30), today)
            except RuntimeError:
                # Never had a parquet AND no NSE series works: a stale entry in
                # the NIFTY500 list for a stock that no longer trades
                # (JBCHEPHARM). Permanent, so report it as such rather than as a
                # failure that would recur every day and mask a real breakage.
                return {"symbol": symbol, "status": "delisted", "added": 0,
                        "last": None}
        fresh = _fetch_range(fy, fyers_sym, date(FIRST_YEAR, 1, 1), today)
        if fresh.empty:
            return {"symbol": symbol, "status": "no-data", "added": 0}
        if not is_index:
            fresh["symbol"] = fyers_sym
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        fresh.to_parquet(path)
        return {"symbol": symbol, "status": "rebuilt", "added": len(fresh),
                "last": str(fresh.index[-1].date())}

    last = stored.index.max().date()
    if last >= today:
        return {"symbol": symbol, "status": "current", "added": 0, "last": str(last)}

    frm = last - timedelta(days=OVERLAP_DAYS)
    try:
        fresh = _fetch_range(fy, fyers_sym, frm, today)
    except RuntimeError as e:
        if is_index or "auth" in str(e).lower():
            raise
        try:
            fyers_sym = _resolve_fyers_symbol(fy, symbol, frm, today)  # -EQ -> -BE
        except RuntimeError:
            # No NSE series works at all -> delisted, renamed or merged (confirmed
            # for JBCHEPHARM, which is absent from Kotak's scrip master too).
            # This is a PERMANENT state, not a transient error: reporting it as a
            # failure every single day would bury the day a live symbol breaks.
            # The stale parquet is left in place; the signal engine's freshness
            # rule drops it from the valid universe on its own.
            return {"symbol": symbol, "status": "delisted", "added": 0,
                    "last": str(last)}
        fresh = _fetch_range(fy, fyers_sym, frm, today)
    if fresh.empty:
        return {"symbol": symbol, "status": "no-new", "added": 0, "last": str(last)}

    if _readjusted(stored, fresh):
        # Fyers rewrote the past -> the stored history is now WRONG. Refetch all.
        full_hist = _fetch_range(fy, fyers_sym, date(FIRST_YEAR, 1, 1), today)
        if full_hist.empty:
            return {"symbol": symbol, "status": "readjust-failed", "added": 0}
        if not is_index:
            full_hist["symbol"] = fyers_sym
        full_hist.to_parquet(path)
        return {"symbol": symbol, "status": "READJUSTED", "added": len(full_hist),
                "last": str(full_hist.index[-1].date())}

    new = fresh[fresh.index > stored.index.max()]
    if new.empty:
        return {"symbol": symbol, "status": "no-new", "added": 0, "last": str(last)}
    if not is_index:
        new = new.copy()
        new["symbol"] = fyers_sym
    merged = pd.concat([stored, new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    merged.to_parquet(path)
    return {"symbol": symbol, "status": "appended", "added": int(len(new)),
            "last": str(merged.index[-1].date()), "series": fyers_sym}


def refresh(symbols=None, full: bool = False, verbose: bool = True) -> dict:
    """Refresh the index plus every symbol parquet. Never raises per-symbol."""
    from deployment.nifty500_symbols import NIFTY500

    fy = _connect()
    todo = list(symbols) if symbols else list(NIFTY500)
    targets = [INDEX_SYMBOL] + todo
    report = {"started": datetime.now().isoformat(timespec="seconds"),
              "counts": {}, "readjusted": [], "series_changed": [],
              "delisted": [], "failed": [], "n": len(targets)}

    for i, sym in enumerate(targets, 1):
        try:
            r = refresh_symbol(fy, sym, full=full)
            st = r["status"]
            report["counts"][st] = report["counts"].get(st, 0) + 1
            if r.get("series") and r["series"] != f"NSE:{sym}-EQ" and sym != INDEX_SYMBOL:
                report["series_changed"].append(f"{sym} -> {r['series']}")
                if verbose:
                    print(f"  [{i}/{len(targets)}] {sym}: SERIES CHANGED -> {r['series']}",
                          flush=True)
            if st == "delisted":
                report["delisted"].append(sym)
            if st == "READJUSTED":
                report["readjusted"].append(sym)
                if verbose:
                    print(f"  [{i}/{len(targets)}] {sym}: RE-ADJUSTED — full history refetched",
                          flush=True)
            elif verbose and st in ("appended", "rebuilt"):
                print(f"  [{i}/{len(targets)}] {sym}: {st} +{r['added']} -> {r.get('last')}",
                      flush=True)
        except Exception as e:
            report["failed"].append(f"{sym}: {type(e).__name__}: {e}")
            report["counts"]["failed"] = report["counts"].get("failed", 0) + 1
            if verbose:
                print(f"  [{i}/{len(targets)}] {sym}: FAILED {type(e).__name__}: {e}", flush=True)
            if "auth" in str(e).lower():
                report["aborted"] = "Fyers auth failed — stopping rather than "\
                                    "hammering the API with a dead token"
                break

    report["finished"] = datetime.now().isoformat(timespec="seconds")
    try:
        idx = pd.read_parquet(DATA_DIR / INDEX_FILE)
        report["index_last"] = str(idx.index.max().date())
    except Exception:
        report["index_last"] = None
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", help="comma-separated, e.g. RELIANCE,HFCL")
    ap.add_argument("--full", action="store_true", help="rebuild full history")
    a = ap.parse_args()
    syms = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else None
    rep = refresh(symbols=syms, full=a.full)
    print("\n" + "=" * 60)
    print(f"  {rep['counts']}")
    if rep.get("delisted"):
        print(f"  DELISTED/renamed (stale parquet left in place): "
              f"{', '.join(rep['delisted'])}")
    if rep.get("series_changed"):
        print(f"  SERIES CHANGED: {', '.join(rep['series_changed'])}")
    if rep["readjusted"]:
        print(f"  RE-ADJUSTED (history rewritten): {', '.join(rep['readjusted'])}")
    if rep["failed"]:
        print(f"  failures ({len(rep['failed'])}):")
        for f in rep["failed"][:10]:
            print(f"    {f}")
    print(f"  index last date: {rep['index_last']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
