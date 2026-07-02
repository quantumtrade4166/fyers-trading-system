"""
live_tick_engine.py  —  V2 (tick-built) combined-premium engine
================================================================

STANDALONE process with its OWN Fyers WebSocket. Builds the combined-premium
5-min candles from the live TICK stream of the day's two option legs (per index),
giving tick-exact high/low — unlike V1 which reconstructs from 1-minute history.

Writes a parallel archive `{date}_{index}_V2.json` (same schema as V1 + version),
so the web app can show V1 vs V2 side by side and we can verify which matches
iCharts. PAPER ONLY — this module never places orders.

SAFETY: deliberately separate from deployment/live_feed.py (the StatArb feed) so
it can't disturb that working production socket. Run as its own process during
market hours, after 9:20 (needs the day's strike selection):

    python live_trading_options/strangle_strategy/live_tick_engine.py

NOTE: cannot be tested outside market hours (no ticks). First live run is the test.
"""

import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import time
import threading
import datetime as dt
from pathlib import Path

import pandas as pd
from fyers_apiv3.FyersWebsocket import data_ws

ROOT = Path(__file__).resolve().parent
sys.path.append(str(ROOT))

from core.fyers_client import load_raw_token, CLIENT_ID, token_status
from core.signal_engine import simulate_day, pair_trades, mtm_series
from live_capture import select_and_cache, INDICES
from core import symbol_master
from data.chart_archive import ARCHIVE_DIR

_PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text())
_LOT_SIZES = _PARAMS["lot_sizes"]

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MKT_START = dt.time(9, 15)
MKT_END   = dt.time(15, 30)
BUCKET_MIN = 5


def _floor_5min(t: dt.datetime) -> dt.datetime:
    return t.replace(minute=(t.minute // BUCKET_MIN) * BUCKET_MIN, second=0, microsecond=0)


class IndexBook:
    """Per-index live combined-candle builder from CE+PE ticks."""

    def __init__(self, index: str, ce_sym: str, pe_sym: str, otm_level, meta: dict):
        self.index, self.ce_sym, self.pe_sym = index, ce_sym, pe_sym
        self.otm_level, self.meta = otm_level, meta
        self.ltp = {ce_sym: None, pe_sym: None}
        self.cumvol = {ce_sym: 0.0, pe_sym: 0.0}
        self.cur_bucket = None        # datetime of current 5-min bucket
        self.o = self.h = self.l = self.c = None
        self.bucket_start_cumvol = 0.0
        self.candles: list[dict] = []  # finalized candles
        self.lock = threading.Lock()
        # resume support: if the engine restarts mid-day, the bucket that was still
        # forming before the restart is restored here and CONTINUED from live ticks
        # (its true bucket-open / high / low are preserved, never reset to V1).
        self._resume_bucket = None    # dt of the forming bucket to resume, or None
        self._resume_ohlc = None      # (o, h, l, c) of that bucket at restart
        self._resume_vol = 0.0        # volume already accumulated in that bucket

    def seed_closed(self, candles: list[dict]):
        """Restore already-finalized candles from a prior archive so VWAP
        accumulates from 9:15 and past candles survive a restart forever."""
        self.candles = [{"time": c["time"], "open": c["open"], "high": c["high"],
                         "low": c["low"], "close": c["close"], "volume": c["volume"]}
                        for c in candles]

    def seed_forming(self, candle: dict, bucket: dt.datetime):
        """Restore the bucket that was still forming at restart, to be CONTINUED by
        live ticks (its open/high/low from before the restart are kept)."""
        self._resume_bucket = bucket
        self._resume_ohlc = (candle["open"], candle["high"], candle["low"], candle["close"])
        self._resume_vol = float(candle.get("volume", 0) or 0)

    def _combined(self):
        a, b = self.ltp[self.ce_sym], self.ltp[self.pe_sym]
        return None if (a is None or b is None) else a + b

    def on_tick(self, sym: str, ltp: float, cumvol: float):
        if sym not in self.ltp:
            return
        with self.lock:
            self.ltp[sym] = ltp
            if cumvol is not None:
                self.cumvol[sym] = cumvol
            comb = self._combined()
            if comb is None:
                return
            now = dt.datetime.now(IST).replace(tzinfo=None)
            if not (MKT_START <= now.time() <= MKT_END):
                return
            bucket = _floor_5min(now)
            if self.cur_bucket is None:
                if self._resume_bucket is not None and bucket == self._resume_bucket:
                    self._resume_bucket_open(bucket, comb)   # continue the pre-restart candle
                else:
                    if self._resume_bucket is not None:      # it fully elapsed during downtime
                        self._finalize_resume()
                    self._open_bucket(bucket, comb)
            elif bucket != self.cur_bucket:
                self._finalize_bucket()
                self._open_bucket(bucket, comb)
            else:
                self.h = max(self.h, comb)
                self.l = min(self.l, comb)
                self.c = comb

    def _open_bucket(self, bucket, comb):
        self.cur_bucket = bucket
        self.o = self.h = self.l = self.c = comb
        self.bucket_start_cumvol = self.cumvol[self.ce_sym] + self.cumvol[self.pe_sym]

    def _resume_bucket_open(self, bucket, comb):
        """Re-open a bucket that was forming before a restart: keep its prior open/
        high/low, extend high/low/close with the tick, and set the volume baseline
        so this bucket's volume = prior partial + volume since restart."""
        o, h, l, _ = self._resume_ohlc
        self.cur_bucket = bucket
        self.o, self.h, self.l, self.c = o, max(h, comb), min(l, comb), comb
        total_now = self.cumvol[self.ce_sym] + self.cumvol[self.pe_sym]
        self.bucket_start_cumvol = total_now - self._resume_vol
        self._resume_bucket = self._resume_ohlc = None
        self._resume_vol = 0.0

    def _finalize_resume(self):
        """The pre-restart forming bucket fully elapsed before the first tick —
        persist it as-is (partial) rather than dropping it and leaving a gap."""
        o, h, l, c = self._resume_ohlc
        self.candles.append({"time": self._resume_bucket.strftime("%H:%M"),
                             "open": round(o, 2), "high": round(h, 2),
                             "low": round(l, 2), "close": round(c, 2),
                             "volume": int(self._resume_vol)})
        self._resume_bucket = self._resume_ohlc = None
        self._resume_vol = 0.0

    def _finalize_bucket(self):
        vol = max(0.0, (self.cumvol[self.ce_sym] + self.cumvol[self.pe_sym]) - self.bucket_start_cumvol)
        self.candles.append({
            "time": self.cur_bucket.strftime("%H:%M"),
            "open": round(self.o, 2), "high": round(self.h, 2),
            "low": round(self.l, 2), "close": round(self.c, 2),
            "volume": int(vol),
        })

    def snapshot_candles(self) -> list[dict]:
        """Finalized candles + the still-forming bucket (so the chart is near-live)."""
        with self.lock:
            out = list(self.candles)
            if self.cur_bucket is not None:
                vol = max(0.0, (self.cumvol[self.ce_sym] + self.cumvol[self.pe_sym]) - self.bucket_start_cumvol)
                out.append({"time": self.cur_bucket.strftime("%H:%M"),
                            "open": round(self.o, 2), "high": round(self.h, 2),
                            "low": round(self.l, 2), "close": round(self.c, 2),
                            "volume": int(vol)})
            elif self._resume_bucket is not None:      # restarted, awaiting first tick
                o, h, l, c = self._resume_ohlc
                out.append({"time": self._resume_bucket.strftime("%H:%M"),
                            "open": round(o, 2), "high": round(h, 2),
                            "low": round(l, 2), "close": round(c, 2),
                            "volume": int(self._resume_vol)})
            return out

    def write_archive(self, date_str: str):
        candles = self.snapshot_candles()
        if not candles:
            return
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(date_str + " " + df["time"])
        df["typ"] = (df.high + df.low + df.close) / 3
        df["pv"] = df.typ * df.volume
        cv = df.volume.cumsum().replace(0, pd.NA)
        df["vwap"] = (df.pv.cumsum() / cv).ffill().fillna(df.close).round(2)
        df["is_red"] = df.close < df.open
        df["below_vwap"] = df.close < df.vwap
        df["above_vwap"] = df.close > df.vwap

        events = simulate_day(df)
        lot = symbol_master.lot_size(self.index) if False else self.meta.get("lot_size", 1)
        pnl = pair_trades(events, lot_size=lot, lots=1)
        pnl["mtm"] = mtm_series(df, events, lot_size=lot, lots=1)

        rec = {
            "date": date_str, "index": self.index, "version": "V2",
            "ce_symbol": self.ce_sym, "pe_symbol": self.pe_sym, "otm_level": self.otm_level,
            "captured_at": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
            "n_candles": len(candles),
            "candles": [{**c, "vwap": float(df.loc[i, "vwap"])} for i, c in enumerate(candles)],
            "events": events, "pnl": pnl, "selection": self.meta,
        }
        (ARCHIVE_DIR / f"{date_str}_{self.index}_V2.json").write_text(json.dumps(rec, indent=2))


# ── module-level engine ───────────────────────────────────────────────────
_books: dict[str, IndexBook] = {}
_sym_to_book: dict[str, IndexBook] = {}
_ws = None
_last_tick = None          # time.monotonic() of the last processed tick


def _on_message(msg):
    # Bulletproof: a single bad message/tick must NEVER escape and kill the WS
    # read loop (that's one way the feed can silently stall). Skip and continue.
    global _last_tick
    try:
        ticks = []
        if isinstance(msg, dict):
            d = msg.get("d")
            ticks = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [msg])
        elif isinstance(msg, list):
            ticks = msg
        for t in ticks:
            try:
                if not isinstance(t, dict):
                    continue
                sym = t.get("symbol")
                ltp = t.get("ltp")
                if not sym or ltp is None:
                    continue
                vol = t.get("vol_traded_today", t.get("volume"))
                book = _sym_to_book.get(sym)
                if book:
                    book.on_tick(sym, float(ltp), float(vol) if vol is not None else None)
                    _last_tick = time.monotonic()
            except Exception as e:
                print(f"  [V2] tick skipped: {e}")
    except Exception as e:
        print(f"  [V2] on_message error: {e}")


def _on_open():
    syms = list(_sym_to_book.keys())
    print(f"  [V2] WS connected — subscribing {len(syms)} legs: {syms}")
    _ws.subscribe(symbols=syms, data_type="SymbolUpdate")


def _on_error(m): print(f"  [V2] WS error: {m}")
def _on_close(m): print(f"  [V2] WS closed: {m}")


def _seed_book(book: "IndexBook", idx: str, date_str: str):
    """Seed a freshly-(re)started engine so a mid-day restart NEVER loses data.

    Prefers this engine's OWN previously-written V2 candles (tick-exact) and only
    falls back to V1 (1-min) for buckets V2 never captured. The bucket that was
    still forming at the last V2 write is RESUMED from live ticks (its open/high/
    low are kept). Net effect: once V2 has written a candle, it survives forever —
    a crash / WS stall / dashboard restart no longer reverts it to V1 wicks."""
    def _load(path):
        try:
            return json.loads(path.read_text())["candles"] if path.exists() else []
        except Exception:
            return []

    now_dt = dt.datetime.now(IST).replace(tzinfo=None)
    cur_bucket_dt = _floor_5min(now_dt)
    now_bkt = cur_bucket_dt.strftime("%H:%M")

    v1 = _load(ARCHIVE_DIR / f"{date_str}_{idx}.json")
    v2 = _load(ARCHIVE_DIR / f"{date_str}_{idx}_V2.json")

    # V2's LAST stored candle is the one that was still forming at the last write;
    # everything before it is properly finalized (tick-exact).
    v2_forming = v2[-1] if v2 else None
    v2_final = v2[:-1] if v2 else []

    closed: dict[str, dict] = {}
    for c in v1:                                    # base layer: V1 (1-min history)
        if c["time"] < now_bkt:
            closed[c["time"]] = c
    for c in v2_final:                              # override with tick-exact V2
        if c["time"] < now_bkt:
            closed[c["time"]] = c

    forming = None
    if v2_forming is not None:
        if v2_forming["time"] == now_bkt:
            forming = v2_forming                    # still forming now → resume live
        elif v2_forming["time"] < now_bkt and v2_forming["time"] not in closed:
            closed[v2_forming["time"]] = v2_forming  # elapsed & V1 lacks it → keep partial

    book.seed_closed([closed[t] for t in sorted(closed)])
    if forming is not None:
        book.seed_forming(forming, cur_bucket_dt)
    print(f"  [V2] {idx}: seeded {len(book.candles)} closed candles"
          + (f" + resuming {forming['time']}" if forming else "")
          + f" (from V2={len(v2)}, V1={len(v1)})")


def build_books(date_str: str):
    """Resolve the day's strikes (cached) and create a book per index."""
    from core.fyers_client import get_client
    client = get_client()
    for idx in INDICES:
        try:
            pick = select_and_cache(client, idx, date_str)
        except Exception as e:
            print(f"  [V2] {idx} strike resolve failed: {e}")
            continue
        meta = {k: pick.get(k) for k in ("spot", "atm", "otm_level", "ce_strike",
                "pe_strike", "combined_premium", "threshold", "dte")}
        meta["lot_size"] = _LOT_SIZES.get(idx, 1)
        book = IndexBook(idx, pick["ce_symbol"], pick["pe_symbol"], pick["otm_level"], meta)
        _seed_book(book, idx, date_str)
        _books[idx] = book
        _sym_to_book[pick["ce_symbol"]] = book
        _sym_to_book[pick["pe_symbol"]] = book
        print(f"  [V2] {idx}: {pick['ce_symbol']} + {pick['pe_symbol']}")


def _push_sheets_eod(date_str: str):
    """Best-effort EOD push of the day's V2 P&L to Google Sheets (if enabled in
    parameters.json). Never raises — a Sheets/network failure must not affect the
    engine or its archive write."""
    if not _PARAMS.get("google_sheets", {}).get("enabled"):
        return
    try:
        from reporting.sheets_logger import log_paper_day
        log_paper_day(date_str, list(_books.keys()))
    except Exception as e:
        print(f"  [V2] sheets push failed: {e}")


def _writer_loop(date_str: str, every: int = 15):
    import os, subprocess
    STALL_SECS = 90
    while True:
        time.sleep(every)
        now = dt.datetime.now(IST)
        if now.time() > dt.time(15, 35):
            for b in _books.values():
                b.write_archive(date_str)
            _push_sheets_eod(date_str)
            print("  [V2] market closed — final archive written, exiting.")
            return
        # Self-heal a SILENT WS stall: if ticks were flowing but stopped for
        # STALL_SECS during market hours, don't sit as a zombie — launch a fresh
        # engine (re-seeds + reconnects) and exit. (Root defence; the dashboard
        # watchdog is the backup.)
        if (_last_tick is not None
                and (time.monotonic() - _last_tick) > STALL_SECS
                and dt.time(9, 20) < now.time() < dt.time(15, 30)):
            print(f"  [V2] tick stall >{STALL_SECS}s — restarting engine")
            try:
                subprocess.run(["schtasks", "/Run", "/TN", "StrangleV2Engine"], capture_output=True)
            finally:
                os._exit(1)
        for b in _books.values():
            try:
                b.write_archive(date_str)
            except Exception as e:
                print(f"  [V2] write error {b.index}: {e}")


def main():
    global _ws
    st = token_status()
    print(f"  [V2] token date={st['date']} valid={st['valid']}")
    if not st["valid"]:
        print("  [V2] token invalid — aborting."); return
    date_str = dt.date.today().isoformat()
    build_books(date_str)
    if not _sym_to_book:
        print("  [V2] no books (run after 9:20 on a trading day) — aborting."); return

    _ws = data_ws.FyersDataSocket(
        access_token=f"{CLIENT_ID}:{load_raw_token()}",
        log_path="", litemode=False, write_to_file=False, reconnect=True,
        on_connect=_on_open, on_close=_on_close, on_error=_on_error, on_message=_on_message,
    )
    threading.Thread(target=_writer_loop, args=(date_str,), daemon=True, name="V2writer").start()
    _ws.connect()


if __name__ == "__main__":
    main()
