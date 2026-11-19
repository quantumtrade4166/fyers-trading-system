"""
strategy_builder/practice_ticks.py — build a PRACTICE DAY from the tick recorder (real ticks, real bid/ask).
==========================================================================================================

Source: live_trading_options/tick_recorder (VPS task TickRecorder, since 13-Nov-2026) — every tick of NIFTY and
SENSEX options (3 expiries, +/-12 % strikes) + the index, with LTP, best bid and best ask, in data/ticks/{date}/.
That is far better than Breeze's 1-minute candles: the replay shows the REAL tick path and paper fills hit the
REAL bid/ask, nothing walked or modelled.

The pack (same folder as practice_data.py packs, read by practice.Replay):
    meta.json          index, date, nearest expiry, strikes, "ticks": true
    ticks.parquet      strike, type, s (seconds after 09:15), ltp, bid, ask — one update per 0.2 s per strike at most
    min.parquet        1-minute OHLC + OI + volume from the ticks (chain OI column, fallback pricing)
    spot.parquet       index 1-minute OHLC          spot_ticks.parquet   index s, ltp (0.2 s)

Kept small on purpose: nearest expiry only (owner's rule) and ATM +/- max(20, day's travel + 6) strikes.

    .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.practice_ticks --index SENSEX --date 2026-11-19
    .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.practice_ticks --today      (both indices, task)
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from live_trading_options.strategy_builder import config as C
from live_trading_options.strategy_builder import practice_data as PD

TICKS_ROOT = C.ROOT / "data" / "ticks"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BUCKET_MS = 200            # keep the last update per contract per 0.2 s
OPEN_S, CLOSE_S = 0, 375 * 60


def have_ticks(date: str, index: str, root: Path = TICKS_ROOT) -> bool:
    d = Path(root) / date
    return d.exists() and any(d.glob(f"{index}_*.tcol"))


def _thin(t_ms, k, *cols):
    """Keep the LAST row per (contract, 0.2 s bucket). Input sorted by time."""
    bucket = (t_ms // BUCKET_MS).astype(np.int64)
    order = np.lexsort((t_ms, bucket, k))               # by contract, then bucket, then time
    k2, b2 = k[order], bucket[order]
    last = np.ones(len(order), bool)
    last[:-1] = (k2[1:] != k2[:-1]) | (b2[1:] != b2[:-1])
    sel = order[last]
    sel = sel[np.argsort(t_ms[sel], kind="stable")]
    return (t_ms[sel], k[sel]) + tuple(c[sel] for c in cols)


def build(index: str, date: str, root: Path = TICKS_ROOT, log=print) -> dict:
    from live_trading_options.tick_recorder import store
    t0 = time.time()
    contracts, keys, c = store.read_day_arrays(date, index, Path(root))
    if not len(c["t"]):
        raise RuntimeError(f"no recorded ticks for {index} {date}")
    info = [contracts[k] for k in keys]
    base = dt.datetime.fromisoformat(f"{date}T09:15:00").replace(tzinfo=IST)
    s_all = c["t"].astype(np.float64) / 1000.0 - base.timestamp()        # seconds after 09:15 IST
    inday = (s_all >= OPEN_S) & (s_all < CLOSE_S)

    # ── index (spot) ──
    ix = [i for i, x in enumerate(info) if x.get("kind") == "INDEX"]
    if not ix:
        raise RuntimeError("no index ticks recorded")
    m = inday & (c["k"] == ix[0]) & (c["ltp"] > 0)
    sp_t, sp_px = s_all[m], c["ltp"][m] / 100.0
    if len(sp_t) < 100:
        raise RuntimeError(f"only {len(sp_t)} index ticks in market hours — day incomplete?")
    step = C.STRIKE_STEP[index]
    open_px, hi, lo = float(sp_px[0]), float(sp_px.max()), float(sp_px.min())
    atm = round(open_px / step) * step

    # ── nearest expiry, strike window ──
    exps = sorted({x["expiry"] for x in info if x.get("kind") in ("CE", "PE") and x["expiry"] >= date})
    if not exps:
        raise RuntimeError("no option expiry on/after the day")
    expiry = exps[0]
    span = max(20, int(max(hi - atm, atm - lo) / step) + 6)
    lo_k, hi_k = atm - span * step, atm + span * step
    want = np.array([x.get("kind") in ("CE", "PE") and x["expiry"] == expiry and lo_k <= float(x["strike"]) <= hi_k
                     for x in info])
    m = inday & want[c["k"]]
    t_ms = c["t"][m].astype(np.int64)
    k = c["k"][m].astype(np.int64)
    ltp, bid, ask = c["ltp"][m], c["bid"][m], c["ask"][m]
    oi, vol = c["oi"][m].astype(np.int64), c["vol"][m].astype(np.int64)
    raw = len(t_ms)
    t_ms, k, ltp, bid, ask, oi, vol = _thin(t_ms, k, ltp, bid, ask, oi, vol)
    s = t_ms / 1000.0 - base.timestamp()
    px = lambda a: np.where(a > 0, a / 100.0, np.nan).astype(np.float32)
    strike = np.array([float(info[i]["strike"]) for i in k])
    typ = np.array([info[i]["kind"] for i in k])
    ticks = pd.DataFrame({"strike": strike, "type": typ, "s": s.astype(np.float32),
                          "ltp": px(ltp), "bid": px(bid), "ask": px(ask)})

    # ── 1-minute candles + OI per contract (chain columns, fallback) ──
    minute = (s // 60).astype(int)
    df = pd.DataFrame({"strike": strike, "type": typ, "m": minute, "ltp": px(ltp).astype(float),
                       "oi": np.where(oi >= 0, oi, np.nan), "vol": np.where(vol >= 0, vol, np.nan)})
    df = df.dropna(subset=["ltp"])
    g = df.groupby(["strike", "type", "m"])
    mins = g["ltp"].agg(o="first", h="max", l="min", c="last").join(g["oi"].last()).join(g["vol"].agg(lambda v: v.max() - v.min()))
    mins = mins.reset_index()
    mins["t"] = pd.Timestamp(f"{date} 09:15:00") + pd.to_timedelta(mins["m"] * 60, unit="s")
    mins = mins[["strike", "type", "t", "o", "h", "l", "c", "oi", "vol"]]

    sp_df = pd.DataFrame({"m": (sp_t // 60).astype(int), "px": sp_px})
    sg = sp_df.groupby("m")["px"].agg(o="first", h="max", l="min", c="last").reset_index()
    sg["t"] = pd.Timestamp(f"{date} 09:15:00") + pd.to_timedelta(sg["m"] * 60, unit="s")
    spot = sg[["t", "o", "h", "l", "c"]]
    st_ms, _, st_px = _thin((sp_t * 1000).astype(np.int64), np.zeros(len(sp_t), np.int64), sp_px)
    spot_ticks = pd.DataFrame({"s": (st_ms / 1000.0).astype(np.float32), "ltp": st_px.astype(np.float32)})

    out = PD.pack_dir(index, date)
    out.mkdir(parents=True, exist_ok=True)
    for f in ("sec.parquet",):                      # a Breeze HD file would shadow the real ticks
        (out / f).unlink(missing_ok=True)
    ticks.to_parquet(out / "ticks.parquet", index=False)
    mins.to_parquet(out / "min.parquet", index=False)
    spot.to_parquet(out / "spot.parquet", index=False)
    spot_ticks.to_parquet(out / "spot_ticks.parquet", index=False)
    meta = {"index": index, "date": date, "expiry": expiry, "step": step, "lot": C.LOT_SIZE[index],
            "atm_open": atm, "open": open_px, "strikes": sorted(float(x) for x in np.unique(strike)),
            "seconds": [], "ticks": True, "tick_rows": int(len(ticks)), "raw_ticks": int(raw),
            "built": time.strftime("%Y-%m-%d %H:%M:%S"), "source": "Kotak tick recorder — real ticks + bid/ask"}
    (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    log(f"{index} {date}: expiry {expiry}, {len(meta['strikes'])} strikes, {raw:,} ticks -> {len(ticks):,} "
        f"(0.2 s), spot {open_px:.1f} [{lo:.0f}-{hi:.0f}] in {time.time() - t0:.0f}s")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", choices=list(C.INDICES))
    ap.add_argument("--date")
    ap.add_argument("--root", default=str(TICKS_ROOT))
    ap.add_argument("--today", action="store_true", help="both indices, today's IST date (the 15:50 task)")
    a = ap.parse_args()
    if a.today:
        d = dt.datetime.now(IST).date().isoformat()
        rc = 0
        for i in C.INDICES:
            try:
                build(i, d, Path(a.root)) if have_ticks(d, i, Path(a.root)) else print(f"{i} {d}: no ticks recorded")
            except Exception as e:
                rc = 1
                print(f"{i} {d}: FAILED {type(e).__name__}: {e}")
        sys.exit(rc)
    build(a.index, a.date, Path(a.root))


if __name__ == "__main__":
    main()
