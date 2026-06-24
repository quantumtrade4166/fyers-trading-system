"""
strangle_system/data/chain_collector.py
========================================
Daily option-chain SNAPSHOT collector for the strangle decision system.

WHY THIS EXISTS
---------------
Fyers serves NO historical option chain — `optionchain` only ever returns the
chain *right now*. A snapshot missed at close is unrecoverable. Because the
user chose "forward-accumulate only" (no historical IV source), this collector
is the foundation of the entire system: it must run daily and never silently
miss a day.

WHAT IT CAPTURES
----------------
For each active underlying (NIFTY on NSE, SENSEX on BSE) and the nearest few
expiries, one row per strike-side (CE / PE):

    date, underlying, expiry, strike, opt_type, spot,
    ltp, bid, ask, volume, oi, prev_oi,
    iv, delta, gamma, theta, vega,          # from greeks=1 (real, not BS-inverted)
    india_vix, captured_at

Stored as:  data/chain_snapshots/{UNDERLYING}/{YYYY-MM-DD}.parquet
Then pushed to Google Drive via service account (headless, self-confirming).

RESILIENCE MODEL
----------------
- Scheduled to run at 15:05 / 15:15 / 15:25 IST (market live → real bid/ask).
- Latest SUCCESSFUL capture of the day overwrites the day's file (atomic write).
  A failed later run never clobbers a good earlier one.
- Manifest records status + drive_uploaded per (underlying, date) → powers a
  health check / dashboard "data collection" panel.
- A crash only loses a day if it spans the ENTIRE close window.

FIELD MAPPING IS VERIFIED, NOT GUESSED
--------------------------------------
The exact key names in the Fyers v3 response are confirmed via `--probe` against
one live call (run on the VPS with a fresh token). All extraction goes through
`_pick()` with candidate key names + a centralized `parse_options_chain()`, so
locking the mapping is a one-place edit.

USAGE
-----
    python -m strangle_system.data.chain_collector --probe          # dump raw JSON, lock mapping
    python -m strangle_system.data.chain_collector                  # capture ACTIVE_UNDERLYINGS
    python -m strangle_system.data.chain_collector --underlyings NIFTY
    python -m strangle_system.data.chain_collector --no-drive       # skip Drive push (local dev)
"""

import argparse
import json
import logging
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# Project imports (run as a module: python -m strangle_system.data.chain_collector)
import sys
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.append(str(_PROJECT_ROOT))

from strangle_system import config
from auth.fyers_auth import get_fyers_client

config.reconfigure_stdout()

config.LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_DIR / "chain_collector.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Canonical columns for the snapshot parquet (stable schema across versions).
SNAPSHOT_COLUMNS = [
    "date", "underlying", "expiry", "strike", "opt_type", "spot",
    "ltp", "bid", "ask", "volume", "oi", "prev_oi",
    "iv", "delta", "gamma", "theta", "vega",
    "india_vix", "symbol", "captured_at",
]


# ──────────────────────────────────────────────────────────────────────────
# Defensive extraction helpers
# ──────────────────────────────────────────────────────────────────────────
def _pick(row: dict, *keys, default=None):
    """Return the first present, non-None value among candidate keys."""
    for k in keys:
        if k in row and row[k] is not None:
            return row[k]
    return default


def _to_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default=None):
    f = _to_float(v, None)
    return int(f) if f is not None else default


# ──────────────────────────────────────────────────────────────────────────
# Fyers calls
# ──────────────────────────────────────────────────────────────────────────
def fetch_chain(fyers, symbol: str, expiry_ts: Optional[int] = None,
                strikecount: int = config.CHAIN_STRIKECOUNT,
                greeks: int = config.CHAIN_GREEKS) -> dict:
    """
    One `optionchain` call. expiry_ts=None/"" → current (nearest) expiry.
    Returns the raw response dict (caller checks response['s']).
    """
    data = {
        "symbol": symbol,
        "strikecount": strikecount,
        "timestamp": "" if expiry_ts in (None, "") else str(expiry_ts),
    }
    if greeks:
        data["greeks"] = greeks
    return fyers.optionchain(data=data)


def _extract_expiries(raw: dict) -> list[tuple[Optional[date], Optional[int]]]:
    """
    From a chain response, return [(expiry_date, expiry_epoch), ...] sorted
    ascending. Parses 'expiryData' defensively; derives date from epoch if the
    string form is missing/odd.
    """
    d = raw.get("data", {}) or {}
    items = d.get("expiryData") or d.get("expiry_data") or []
    out = []
    for it in items:
        epoch = _to_int(_pick(it, "expiry", "expiryTs", "timestamp"))
        dt = None
        ds = _pick(it, "date")
        if ds:
            for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%b-%Y"):
                try:
                    dt = datetime.strptime(str(ds), fmt).date()
                    break
                except ValueError:
                    continue
        if dt is None and epoch is not None:
            dt = datetime.fromtimestamp(epoch, IST).date()
        out.append((dt, epoch))
    out.sort(key=lambda t: (t[1] is None, t[1]))
    return out


def parse_options_chain(raw: dict, underlying: str, expiry_dt: Optional[date],
                        captured_at: datetime) -> pd.DataFrame:
    """
    Convert one chain response into snapshot rows (one per CE/PE strike).

    CENTRAL FIELD MAP — adjust here once `--probe` confirms live key names.
    The index row (option_type == "" / the underlying itself) is used for spot
    and is NOT emitted as an option row.
    """
    d = raw.get("data", {}) or {}
    chain = d.get("optionsChain") or d.get("options_chain") or []

    # India VIX (free inline; captured even though not used for backtesting).
    vix_node = d.get("indiavixData") or d.get("indiaVixData") or {}
    india_vix = _to_float(_pick(vix_node, "ltp", "value", "iv")) if isinstance(vix_node, dict) else None

    # Spot: prefer the index row inside the chain; fall back to data-level field.
    spot = None
    rows = []
    for r in chain:
        otype = str(_pick(r, "option_type", "optionType", default="")).strip().upper()
        if otype not in ("CE", "PE"):
            # Index / underlying row → spot price.
            spot = _to_float(_pick(r, "ltp", "fp", "lp")) or spot
            continue
        rows.append((otype, r))

    if spot is None:
        spot = _to_float(_pick(d, "spot", "underlyingValue", "ltp"))

    out = []
    cap_iso = captured_at.isoformat(timespec="seconds")
    cap_date = captured_at.astimezone(IST).date()
    for otype, r in rows:
        out.append({
            "date": str(cap_date),
            "underlying": underlying,
            "expiry": str(expiry_dt) if expiry_dt else None,
            "strike": _to_float(_pick(r, "strike_price", "strikePrice", "strike")),
            "opt_type": otype,
            "spot": spot,
            "ltp": _to_float(_pick(r, "ltp", "lp")),
            "bid": _to_float(_pick(r, "bid", "bidPrice", "bid_price")),
            "ask": _to_float(_pick(r, "ask", "askPrice", "ask_price")),
            "volume": _to_int(_pick(r, "volume", "vol", "tradedQty")),
            "oi": _to_int(_pick(r, "oi", "openInterest", "open_interest")),
            "prev_oi": _to_int(_pick(r, "prev_oi", "prevOi", "previousOi")),
            # Greeks (greeks=1). Try inline keys and a nested 'greeks' dict.
            "iv": _to_float(_pick(r, "iv", "impliedVolatility",
                                  default=_pick(r.get("greeks", {}) or {}, "iv"))),
            "delta": _to_float(_pick(r, "delta",
                                     default=_pick(r.get("greeks", {}) or {}, "delta"))),
            "gamma": _to_float(_pick(r, "gamma",
                                     default=_pick(r.get("greeks", {}) or {}, "gamma"))),
            "theta": _to_float(_pick(r, "theta",
                                     default=_pick(r.get("greeks", {}) or {}, "theta"))),
            "vega": _to_float(_pick(r, "vega",
                                    default=_pick(r.get("greeks", {}) or {}, "vega"))),
            "india_vix": india_vix,
            "symbol": _pick(r, "symbol", "fyToken"),
            "captured_at": cap_iso,
        })

    df = pd.DataFrame(out, columns=SNAPSHOT_COLUMNS)
    return df


# ──────────────────────────────────────────────────────────────────────────
# Per-underlying capture
# ──────────────────────────────────────────────────────────────────────────
def capture_underlying(fyers, underlying: str,
                       num_expiries: int = config.CHAIN_NUM_EXPIRIES,
                       strikecount: int = config.CHAIN_STRIKECOUNT) -> pd.DataFrame:
    """
    Snapshot the nearest `num_expiries` expiries for one underlying.
    Returns a combined DataFrame (may be empty on failure — fail-safe).
    """
    cfg = config.UNDERLYINGS[underlying]
    symbol = cfg["index_symbol"]
    captured_at = datetime.now(IST)

    # First call (current expiry) also gives the expiry list.
    raw0 = fetch_chain(fyers, symbol, expiry_ts=None, strikecount=strikecount)
    if raw0.get("s") != "ok":
        logger.warning(f"[{underlying}] optionchain failed: {raw0.get('message', raw0)}")
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    expiries = _extract_expiries(raw0)
    if not expiries:
        logger.warning(f"[{underlying}] no expiryData in response.")
        return pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    frames = []
    # Expiry[0] is already in raw0.
    frames.append(parse_options_chain(raw0, underlying, expiries[0][0], captured_at))

    for exp_dt, exp_ts in expiries[1:num_expiries]:
        time.sleep(config.SLEEP_BETWEEN_CALLS)
        raw = fetch_chain(fyers, symbol, expiry_ts=exp_ts, strikecount=strikecount)
        if raw.get("s") != "ok":
            logger.warning(f"[{underlying}] expiry {exp_dt} failed: {raw.get('message')}")
            continue
        frames.append(parse_options_chain(raw, underlying, exp_dt, captured_at))

    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SNAPSHOT_COLUMNS)
    df = df.dropna(subset=["strike"])
    return df


# ──────────────────────────────────────────────────────────────────────────
# Persistence (latest-wins, atomic) + manifest
# ──────────────────────────────────────────────────────────────────────────
def snapshot_path(underlying: str, capture_date: date) -> Path:
    folder = config.CHAIN_SNAPSHOT_DIR / underlying
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{capture_date}.parquet"


def save_snapshot(df: pd.DataFrame, underlying: str, capture_date: date) -> Path:
    """Atomic latest-wins write: write temp then replace the day's file."""
    path = snapshot_path(underlying, capture_date)
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, compression="snappy", index=False)
    tmp.replace(path)  # atomic on same filesystem
    return path


def _load_manifest() -> dict:
    f = config.CHAIN_MANIFEST_FILE
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": None, "snapshots": {}}


def _save_manifest(man: dict):
    config.CHAIN_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    man["last_updated"] = datetime.now(IST).isoformat(timespec="seconds")
    config.CHAIN_MANIFEST_FILE.write_text(json.dumps(man, indent=2), encoding="utf-8")


def _greeks_present(df: pd.DataFrame) -> bool:
    return bool(len(df) and df["iv"].notna().any() and df["gamma"].notna().any())


def _update_manifest(man: dict, underlying: str, capture_date: date,
                     df: pd.DataFrame, drive_ok: Optional[bool], status: str,
                     error: str = None):
    key = f"{underlying}_{capture_date}"
    man.setdefault("snapshots", {})[key] = {
        "underlying": underlying,
        "date": str(capture_date),
        "status": status,
        "rows": int(len(df)),
        "expiries": sorted([e for e in df["expiry"].dropna().unique().tolist()]) if len(df) else [],
        "spot": (float(df["spot"].dropna().iloc[0]) if len(df) and df["spot"].notna().any() else None),
        "greeks_present": _greeks_present(df),
        "captured_at": (df["captured_at"].iloc[0] if len(df) else None),
        "drive_uploaded": drive_ok,
        "error": error,
        "recorded_at": datetime.now(IST).isoformat(timespec="seconds"),
    }


# ──────────────────────────────────────────────────────────────────────────
# Google Drive push (service account; reuse existing storage.gdrive_sync)
# ──────────────────────────────────────────────────────────────────────────
def push_to_drive(local_path: Path, underlying: str) -> Optional[bool]:
    """
    Upload one snapshot to Drive under CHAIN_DRIVE_FOLDER/{underlying}/.
    Returns True on success, False on failure, None if Drive disabled/unconfigured.
    Self-confirming so a silent-sync failure can't hide a data hole.
    """
    if not config.CHAIN_PUSH_TO_DRIVE:
        return None
    try:
        from storage.gdrive_sync import get_drive_service, get_or_create_folder, upload_file
    except Exception as exc:
        logger.warning(f"Drive libs unavailable: {exc}")
        return False
    try:
        svc = get_drive_service()
        root = get_or_create_folder(svc, config.CHAIN_DRIVE_FOLDER)
        sub = get_or_create_folder(svc, underlying, root)
        upload_file(svc, local_path, sub)
        logger.info(f"[{underlying}] Drive upload OK: {local_path.name}")
        return True
    except FileNotFoundError as exc:
        logger.warning(f"Drive credentials missing: {exc}")
        return False
    except Exception as exc:
        logger.error(f"[{underlying}] Drive upload FAILED: {exc}")
        return False


# ──────────────────────────────────────────────────────────────────────────
# Probe mode — lock the field mapping against one live response
# ──────────────────────────────────────────────────────────────────────────
def probe(underlying: str = "NIFTY"):
    fyers = get_fyers_client()
    symbol = config.UNDERLYINGS[underlying]["index_symbol"]
    print(f"\n=== PROBE {underlying} ({symbol}) ===")
    raw = fetch_chain(fyers, symbol, expiry_ts=None, strikecount=3)
    print("response 's':", raw.get("s"), "| code:", raw.get("code"))
    if raw.get("s") != "ok":
        print("message:", raw.get("message"))
        return
    d = raw.get("data", {}) or {}
    print("data top-level keys:", list(d.keys()))
    print("\nexpiryData[:3]:", json.dumps((d.get("expiryData") or [])[:3], indent=2))
    chain = d.get("optionsChain") or []
    print(f"\noptionsChain rows: {len(chain)}")
    if chain:
        print("FIRST ROW keys:", list(chain[0].keys()))
        print("FIRST ROW:", json.dumps(chain[0], indent=2, default=str))
        # show a CE row with greeks if present
        ce = next((r for r in chain if str(r.get("option_type", "")).upper() == "CE"), None)
        if ce:
            print("\nSAMPLE CE ROW:", json.dumps(ce, indent=2, default=str))
    print("\nParsed preview:")
    df = parse_options_chain(raw, underlying, None, datetime.now(IST))
    print(df.head(6).to_string())
    print("\ngreeks_present:", _greeks_present(df))


# ──────────────────────────────────────────────────────────────────────────
# Main run
# ──────────────────────────────────────────────────────────────────────────
def run(underlyings: list[str] = None, push_drive: bool = True,
        num_expiries: int = config.CHAIN_NUM_EXPIRIES,
        strikecount: int = config.CHAIN_STRIKECOUNT):
    underlyings = underlyings or config.ACTIVE_UNDERLYINGS
    fyers = get_fyers_client()
    man = _load_manifest()

    print(f"\n{'='*60}")
    print("  OPTION-CHAIN SNAPSHOT COLLECTOR")
    print(f"{'='*60}")
    print(f"  Time (IST)   : {datetime.now(IST):%Y-%m-%d %H:%M:%S}")
    print(f"  Underlyings  : {underlyings}")
    print(f"  Strikecount  : {strikecount}  | Expiries: {num_expiries}")
    print(f"  Drive push   : {push_drive and config.CHAIN_PUSH_TO_DRIVE}")
    print(f"{'='*60}\n")

    results = []
    for u in underlyings:
        try:
            df = capture_underlying(fyers, u, num_expiries, strikecount)
        except Exception as exc:
            logger.error(f"[{u}] capture crashed: {exc}")
            _update_manifest(man, u, datetime.now(IST).date(),
                             pd.DataFrame(columns=SNAPSHOT_COLUMNS), None, "error", str(exc))
            results.append((u, 0, None))
            continue

        cap_date = datetime.now(IST).date()
        if df.empty:
            logger.warning(f"[{u}] empty snapshot — NOT saved (preserving any earlier capture).")
            _update_manifest(man, u, cap_date, df, None, "no_data")
            results.append((u, 0, None))
            continue

        path = save_snapshot(df, u, cap_date)
        drive_ok = push_to_drive(path, u) if push_drive else None
        _update_manifest(man, u, cap_date, df, drive_ok, "success")
        results.append((u, len(df), drive_ok))
        print(f"  {u}: {len(df)} rows  | expiries={sorted(df['expiry'].dropna().unique())}  "
              f"| greeks={'yes' if _greeks_present(df) else 'NO'}  | drive={drive_ok}")
        time.sleep(config.SLEEP_BETWEEN_CALLS)

    _save_manifest(man)

    print(f"\n{'='*60}\n  DONE")
    for u, n, drive_ok in results:
        flag = "OK" if n else "MISS"
        print(f"  [{flag}] {u}: {n} rows | drive={drive_ok}")
    print(f"{'='*60}\n")
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Daily option-chain snapshot collector")
    ap.add_argument("--probe", action="store_true",
                    help="Dump raw Fyers response to lock the field mapping (1 live call).")
    ap.add_argument("--underlyings", nargs="+", default=None,
                    help="Subset of underlyings, e.g. --underlyings NIFTY")
    ap.add_argument("--expiries", type=int, default=config.CHAIN_NUM_EXPIRIES)
    ap.add_argument("--strikecount", type=int, default=config.CHAIN_STRIKECOUNT)
    ap.add_argument("--no-drive", action="store_true", help="Skip Google Drive push.")
    args = ap.parse_args()

    if args.probe:
        for u in (args.underlyings or ["NIFTY"]):
            probe(u)
    else:
        run(underlyings=args.underlyings, push_drive=not args.no_drive,
            num_expiries=args.expiries, strikecount=args.strikecount)
