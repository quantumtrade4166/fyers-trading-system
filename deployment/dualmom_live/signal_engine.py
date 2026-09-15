"""
dualmom_live/signal_engine.py — the month-end signal, on Fyers data only.

Replaces the yfinance path in deployment/dualmom_paper.py, which is unfit for
live use on three counts proven in the 2026-09 review:
  - it returned ZERO rows for ^NSEI at 16:00 on 2026-08-31 and the rebalance aborted
  - its Nifty history is missing 723 trading days, including all of 2006-mid-2007
  - it adjusted 0 of 6 corporate actions; Fyers adjusted 5 of 6

It also still ran TOP_N = 50 and fractional shares. Both are wrong per the
final spec (TOP_N = 40, whole shares).

The completeness guard matters more than anything else here: the old code only
checked `len(returns) < 10`. A partial download returning 200 of 500 symbols
produces a WRONG BUT ENTIRELY PLAUSIBLE top-40 — and with no human in the loop
it would be traded in two client accounts.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import config as C
from . import rebalance as R

ROOT      = Path(__file__).resolve().parents[2]
DATA_DIR  = ROOT / "Nifty 500 Daily Fyers"
INDEX_PQ  = DATA_DIR / "_NIFTY50_INDEX.parquet"


# ── data ─────────────────────────────────────────────────────────────────────

_price_cache = {"key": None, "df": None}


def _dir_fingerprint():
    """Changes whenever the 16:10 refresh rewrites any parquet."""
    files = [f for f in DATA_DIR.glob("*.parquet") if not f.stem.startswith("_")]
    return (len(files), max((f.stat().st_mtime_ns for f in files), default=0))


def load_prices(symbols=None) -> pd.DataFrame:
    """All 500 closes as one frame, cached in-process until a parquet changes.

    A single dashboard Preview used to re-read all 500 files three or four times
    (month signal date, compute, build_plan...). On the VPS that burned 180 CPU-
    seconds and pushed the request past the dashboard proxy's 10-minute timeout
    the evening before the first live entry. The fingerprint is the newest file
    mtime, so a refresh is picked up on the very next call.
    """
    if symbols is None:
        key = _dir_fingerprint()
        if _price_cache["key"] == key and _price_cache["df"] is not None:
            return _price_cache["df"]
        df = _load_prices_uncached(None)
        _price_cache.update(key=key, df=df)
        return df
    return _load_prices_uncached(symbols)


def _load_prices_uncached(symbols=None) -> pd.DataFrame:
    frames = {}
    for f in DATA_DIR.glob("*.parquet"):
        if f.stem.startswith("_"):
            continue
        if symbols is not None and f.stem not in symbols:
            continue
        d = pd.read_parquet(f, columns=["close"])
        d.index = pd.to_datetime(d.index)
        frames[f.stem] = d["close"]
    if not frames:
        raise RuntimeError(f"no price parquets under {DATA_DIR}")
    return pd.DataFrame(frames).sort_index()


def load_nifty() -> tuple:
    d = pd.read_parquet(INDEX_PQ, columns=["close"])
    d.index = pd.to_datetime(d.index)
    return d["close"], d["close"].rolling(C.MA_PERIOD).mean()


# ── calendar ─────────────────────────────────────────────────────────────────

def is_last_trading_day(prices: pd.DataFrame, on: date = None) -> bool:
    """True when `on` is the final session of its month IN THE DATA — not a
    guess from the weekday, which misfires on exchange holidays."""
    on = on or date.today()
    idx = prices.index
    same = idx[(idx.year == on.year) & (idx.month == on.month)]
    return len(same) > 0 and same[-1].date() == on


# ── the signal ───────────────────────────────────────────────────────────────

def compute(as_of: date = None, prices: pd.DataFrame = None) -> dict:
    """Return the month-end signal. Pure: reads data, places nothing.

    Raises RuntimeError if the universe is too thin to trust — refusing to trade
    is always safer than trading a top-40 built from partial data.
    """
    prices = load_prices() if prices is None else prices
    nifty, ma = load_nifty()

    idx = prices.index
    ts = pd.Timestamp(as_of) if as_of else idx[-1]
    pos = idx.get_indexer([ts], method="ffill")[0]
    if pos < 0:
        raise RuntimeError(f"no price data on or before {ts.date()}")
    rd = idx[pos]

    lb = pos - C.LOOKBACK_DAYS
    if lb < 0:
        raise RuntimeError(f"need {C.LOOKBACK_DAYS} sessions of history before {rd.date()}")

    ni = nifty.index.get_indexer([rd], method="ffill")[0]
    if ni < 0:
        raise RuntimeError(f"no Nifty index data on or before {rd.date()}")
    n_px, n_ma = float(nifty.iloc[ni]), float(ma.iloc[ni])
    if np.isnan(n_ma):
        raise RuntimeError(f"{C.MA_PERIOD}-day MA not available at {rd.date()}")
    signal = "IN" if n_px > n_ma else "OUT"

    cur, past = prices.iloc[pos], prices.iloc[lb]
    valid = (cur.notna() & past.notna() & (past > 0))
    n_valid = int(valid.sum())

    # ── the guard the old engine did not have ────────────────────────────────
    # Relative, not absolute: learn what this dataset normally carries from the
    # trailing window, then demand most of it. That catches a partial download
    # without falsely blocking a genuinely smaller historical universe.
    # `expected` must use the SAME validity rule as n_valid (today's price AND
    # the 252-back price), otherwise it over-counts newly-listed names and
    # falsely blocks. Jan-2008: 223 valid vs 259 same-day -> spurious block.
    ref_lo = max(0, pos - C.UNIVERSE_REF_DAYS)
    expected = 0
    for j in range(ref_lo, pos + 1):
        k = j - C.LOOKBACK_DAYS
        if k < 0:
            continue
        cnt = int((prices.iloc[j].notna() & prices.iloc[k].notna()
                   & (prices.iloc[k] > 0)).sum())
        expected = max(expected, cnt)
    expected = expected or n_valid
    floor = max(C.MIN_UNIVERSE_ABS, int(expected * C.MIN_UNIVERSE_FRAC))
    if n_valid < floor:
        raise RuntimeError(
            f"universe too thin: {n_valid} valid symbols < {floor} "
            f"({C.MIN_UNIVERSE_FRAC:.0%} of the trailing-{C.UNIVERSE_REF_DAYS}-day "
            f"norm of {expected}). Refusing to rebalance — a partial dataset "
            "yields a plausible but WRONG top-40.")

    r12 = (cur[valid] / past[valid] - 1).dropna()
    out = {
        "date": str(rd.date()),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signal": signal,
        "nifty_close": round(n_px, 2),
        "nifty_ma": round(n_ma, 2),
        "gap_pct": round((n_px / n_ma - 1) * 100, 3),
        "universe_valid": n_valid,
        "universe_expected": expected,
        "universe_floor": floor,
        "top_n": C.TOP_N,
        "holdings": [],
    }
    if signal == "OUT":
        return out

    top = r12.nlargest(C.TOP_N)
    raw = {s: max(float(v), 0.001) for s, v in top.items()}
    uncapped = {s: v / sum(raw.values()) for s, v in raw.items()}
    weights = R.cap_weights(raw, C.MAX_WEIGHT)
    assert abs(sum(weights.values()) - 1.0) < 1e-9, sum(weights.values())
    out["max_weight_cap"] = C.MAX_WEIGHT
    for sym, w in weights.items():
        if uncapped[sym] > (C.MAX_WEIGHT or 1.0) + 1e-9:
            out.setdefault("capped", []).append(
                {"symbol": sym, "uncapped_pct": round(uncapped[sym] * 100, 2),
                 "capped_pct": round(w * 100, 2)})
    for rank, (sym, w) in enumerate(
            sorted(weights.items(), key=lambda kv: -raw[kv[0]]), 1):
        out["holdings"].append({
            "rank": rank, "symbol": sym,
            "weight": w,          # FULL precision, post-cap — allocate() consumes this
            "weight_pct": round(w * 100, 3),
            "return_12m_pct": round(float(top[sym]) * 100, 2),
            "price": round(float(cur[sym]), 2),
        })
    return out


def save(sig: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sig, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    s = compute()
    print(f"{s['date']}  signal={s['signal']}  nifty={s['nifty_close']:,.2f} "
          f"vs MA {s['nifty_ma']:,.2f} ({s['gap_pct']:+.2f}%)  "
          f"universe={s['universe_valid']}")
    for h in s["holdings"][:10]:
        print(f"  {h['rank']:>2} {h['symbol']:<13} {h['weight_pct']:>5.2f}%  "
              f"12m {h['return_12m_pct']:>7.1f}%  Rs {h['price']:>9,.2f}")
    if len(s["holdings"]) > 10:
        print(f"  ... {len(s['holdings'])-10} more")
