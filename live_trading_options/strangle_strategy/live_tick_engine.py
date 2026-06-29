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


def _on_message(msg):
    ticks = []
    if isinstance(msg, dict):
        d = msg.get("d")
        ticks = d if isinstance(d, list) else ([d] if isinstance(d, dict) else [msg])
    elif isinstance(msg, list):
        ticks = msg
    for t in ticks:
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


def _on_open():
    syms = list(_sym_to_book.keys())
    print(f"  [V2] WS connected — subscribing {len(syms)} legs: {syms}")
    _ws.subscribe(symbols=syms, data_type="SymbolUpdate")


def _on_error(m): print(f"  [V2] WS error: {m}")
def _on_close(m): print(f"  [V2] WS closed: {m}")


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
        meta = {k: pick.get(k) for k in ("spot", "atm", "otm_level",
                "combined_premium", "threshold", "dte")}
        meta["lot_size"] = _LOT_SIZES.get(idx, 1)
        book = IndexBook(idx, pick["ce_symbol"], pick["pe_symbol"], pick["otm_level"], meta)
        _books[idx] = book
        _sym_to_book[pick["ce_symbol"]] = book
        _sym_to_book[pick["pe_symbol"]] = book
        print(f"  [V2] {idx}: {pick['ce_symbol']} + {pick['pe_symbol']}")


def _writer_loop(date_str: str, every: int = 15):
    while True:
        time.sleep(every)
        now = dt.datetime.now(IST).time()
        if now > dt.time(15, 35):
            for b in _books.values():
                b.write_archive(date_str)
            print("  [V2] market closed — final archive written, exiting.")
            return
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
