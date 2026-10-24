"""
strategy_builder/practice_data.py — build a PRACTICE DAY pack from ICICI Breeze history.
=======================================================================================

One pack = one index on one past day, everything the replay needs and nothing else:

    data/practice/{INDEX}/{YYYY-MM-DD}/
        meta.json      index, date, expiry, step, lot, strikes, which contracts have 1-second data
        spot.parquet   index 1-minute OHLC (09:15-15:29)
        min.parquet    every CE/PE, ATM +/- 20 strikes, nearest expiry: 1-minute OHLC + OI + volume
        sec.parquet    (optional) 1-second closes for ATM +/- 5 strikes — the "HD" upgrade

Cost (Breeze, one worker — concurrency gets the IP blocked):
    1-minute day  ~ 85 calls   ~ 3 min
    1-second HD   ~ 22 contracts x up to 24 calls  ~ 15-20 min
Everything is cached in data/BREEZE_OPTIONS through options.breeze.datasource, so a day is
paid for once and re-packing is free.

The nearest expiry is DISCOVERED (first date >= the day whose ATM call has data) — there is no
SENSEX calendar and NSE/BSE moved weekdays in 2025, so a calendar would be the weak link.

    .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.practice_data --index NIFTY --date 2026-08-12
    .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.practice_data --index SENSEX --date 2026-08-13 --seconds
    .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.practice_data --index NIFTY --list
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from live_trading_options.strategy_builder import config as C

PRACTICE_DIR = C.DATA / "practice"
BREEZE_CODE = {"NIFTY": "NIFTY", "SENSEX": "BSESEN"}
INDEX_EXCH = {"NIFTY": "NSE", "SENSEX": "BSE"}
WIDTH = 20          # strikes each side of the opening ATM (1-minute)
HD_WIDTH = 5        # strikes each side for 1-second


def pack_dir(index: str, date: str) -> Path:
    return PRACTICE_DIR / index / date


def read_meta(index: str, date: str) -> dict | None:
    try:
        return json.loads((pack_dir(index, date) / "meta.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def list_packs(index: str | None = None) -> list:
    out = []
    for idx in ([index] if index else C.INDICES):
        root = PRACTICE_DIR / idx
        if not root.exists():
            continue
        for d in sorted(root.iterdir()):
            m = read_meta(idx, d.name)
            if m:
                out.append({"index": idx, "date": d.name, "expiry": m["expiry"],
                            "seconds": bool(m.get("seconds")), "strikes": len(m["strikes"])})
    return out


class Builder:
    def __init__(self, index: str, log=print, fast: bool = False):
        from options.breeze.datasource import BreezeData
        self.index, self.log, self.fast = index, log, fast
        self.code = BREEZE_CODE[index]
        self.step = C.STRIKE_STEP[index]
        self.m = BreezeData(interval="1minute", stock_code=self.code, verbose=False)
        self._s = None
        self._client = None

    def _sec(self):
        if self._s is None:
            from options.breeze.datasource import BreezeData
            self._s = BreezeData(interval="1second", stock_code=self.code, verbose=False)
        return self._s

    def client(self):
        if self._client is None:
            from options.breeze.session import get_client
            self._client = get_client()
        return self._client

    # ── index 1-minute (1 call) ──
    def spot(self, date: str) -> pd.DataFrame:
        if self.index == "NIFTY":
            p = C.ROOT / "data" / "BREEZE_OPTIONS" / "NIFTY" / "index_1min.parquet"
            if p.exists():
                df = pd.read_parquet(p)
                df = df[pd.to_datetime(df["datetime"]).dt.date.astype(str) == date]
                if len(df) > 300:
                    return df[["datetime", "open", "high", "low", "close"]].reset_index(drop=True)
        r = self.client().get_historical_data_v2(
            interval="1minute", from_date=f"{date}T09:15:00.000Z", to_date=f"{date}T15:30:00.000Z",
            stock_code=self.code, exchange_code=INDEX_EXCH[self.index], product_type="cash")
        rows = (r or {}).get("Success") or []
        if not rows:
            raise RuntimeError(f"no {self.index} index data for {date} (holiday?) — {str(r)[:160]}")
        df = pd.DataFrame(rows)
        df["datetime"] = pd.to_datetime(df["datetime"])
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df[["datetime", "open", "high", "low", "close"]].dropna().sort_values("datetime").reset_index(drop=True)

    def bars(self, data, expiry, strike, right, date):
        if self.fast and data is self.m:
            return self._direct(expiry, strike, right, date)
        # Windows briefly locks the manifest while the backup sync reads it (WinError 5) —
        # the parquet is already written, so wait and retry; the retry re-commits the manifest.
        for attempt in range(6):
            try:
                return data.bars(expiry=expiry, strike=int(strike), right=right, trade_date=date)
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(2 + 2 * attempt)

    def _direct(self, expiry, strike, right, date):
        """Bulk mode: one 1-minute request straight to Breeze (375 bars < the 1000 cap), same
        retry/throttle as the downloader, but WITHOUT rewriting the shared year cache after every
        contract — that rewrite was ~60% of the time per day."""
        from options.breeze.downloader import Contract
        dl = self.m._dl
        self.m._ensure_client()
        c = Contract(trade_date=date, expiry_date=expiry, strike_price=int(strike), right=right,
                     stock_code=self.code, exchange_code=self.m.exchange_code)
        base = pd.Timestamp(date).to_pydatetime()
        status, rows, err = dl._fetch_range(c, base.replace(hour=9, minute=15), base.replace(hour=15, minute=30))
        if status == "error":
            raise RuntimeError(f"Breeze {date} {strike}{right[0].upper()} {expiry}: {err}")
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(dl.normalise(rows, c))
        return df.dropna(subset=["datetime", "close"]).sort_values("datetime").reset_index(drop=True)

    def find_expiry(self, date: str, atm: float) -> str:
        d0 = dt.date.fromisoformat(date)
        for k in range(0, 8):
            e = (d0 + dt.timedelta(days=k)).isoformat()
            if dt.date.fromisoformat(e).weekday() > 4:
                continue
            df = self.bars(self.m, e, atm, "call", date)
            if len(df):
                return e
        raise RuntimeError(f"no {self.index} expiry found within 7 days of {date}")

    def build(self, date: str, seconds: bool = False) -> dict:
        t0 = time.time()
        spot = self.spot(date)
        open_px = float(spot["open"].iloc[0])
        hi, lo = float(spot["high"].max()), float(spot["low"].min())
        atm = round(open_px / self.step) * self.step
        self.log(f"{self.index} {date}: open {open_px:.1f} (day {lo:.0f}-{hi:.0f}) ATM {atm}")
        expiry = self.find_expiry(date, atm)
        self.log(f"  nearest expiry {expiry}")
        # widen the window if the day travelled further than +/-WIDTH strikes
        span = max(WIDTH, int(max(hi - atm, atm - lo) / self.step) + 6)
        strikes = [atm + i * self.step for i in range(-span, span + 1)]
        frames, n = [], 0
        for k in strikes:
            for right, typ in (("call", "CE"), ("put", "PE")):
                df = self.bars(self.m, expiry, k, right, date)
                n += 1
                if len(df):
                    frames.append(pd.DataFrame({"strike": float(k), "type": typ,
                                                "t": pd.to_datetime(df["datetime"]),
                                                "o": df["open"].astype(float), "h": df["high"].astype(float),
                                                "l": df["low"].astype(float), "c": df["close"].astype(float),
                                                "oi": pd.to_numeric(df["oi"], errors="coerce"),
                                                "vol": pd.to_numeric(df["volume"], errors="coerce")}))
                if n % 10 == 0:
                    self.log(f"  1-minute {n}/{2 * len(strikes)} contracts")
        if not frames:
            raise RuntimeError("no option bars returned")
        mins = pd.concat(frames, ignore_index=True)
        have = sorted(mins["strike"].unique())
        out = pack_dir(self.index, date)
        out.mkdir(parents=True, exist_ok=True)
        spot.rename(columns={"datetime": "t", "open": "o", "high": "h", "low": "l", "close": "c"}) \
            .to_parquet(out / "spot.parquet", index=False)
        mins.to_parquet(out / "min.parquet", index=False)
        meta = {"index": self.index, "date": date, "expiry": expiry, "step": self.step,
                "lot": C.LOT_SIZE[self.index], "atm_open": atm, "open": open_px,
                "strikes": [float(x) for x in have], "seconds": [], "built": time.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "ICICI Breeze 1-minute"}
        (out / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        self.log(f"  saved {len(mins):,} bars, {len(have)} strikes in {time.time() - t0:.0f}s")
        if seconds:
            self.add_seconds(date)
        return read_meta(self.index, date)

    def add_seconds(self, date: str) -> dict:
        meta = read_meta(self.index, date)
        if not meta:
            raise RuntimeError("build the 1-minute pack first")
        atm = meta["atm_open"]
        sec = self._sec()
        frames, done = [], []
        ks = [atm + i * self.step for i in range(-HD_WIDTH, HD_WIDTH + 1)]
        for i, k in enumerate(ks):
            for right, typ in (("call", "CE"), ("put", "PE")):
                df = self.bars(sec, meta["expiry"], k, right, date)
                if len(df):
                    frames.append(pd.DataFrame({"strike": float(k), "type": typ,
                                                "t": pd.to_datetime(df["datetime"]), "c": df["close"].astype(float)}))
                    done.append(f"{float(k)}|{typ}")
            self.log(f"  1-second {i + 1}/{len(ks)} strikes")
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(pack_dir(self.index, date) / "sec.parquet", index=False)
        meta["seconds"] = done
        meta["source"] = "ICICI Breeze 1-minute + 1-second (ATM±%d)" % HD_WIDTH
        (pack_dir(self.index, date) / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return meta


# ── bulk backfill: every weekday of a range, both indices, newest first ─────────────────────
STATUS_FILE = PRACTICE_DIR / "_batch_status.json"
SKIP_FILE = PRACTICE_DIR / "_skipped.json"          # holidays / days Breeze has nothing for
PRIORITY_FILE = PRACTICE_DIR / "_priority.json"     # a day the page asked for — jumps the queue
LOCK_FILE = PRACTICE_DIR / "_batch.lock"


def _rj(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _wj(p, v):
    PRACTICE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(v, indent=1), encoding="utf-8")
    tmp.replace(p)


def batch_running() -> bool:
    """A live backfill refreshes the lock every day it works on (and every 60 s while waiting)."""
    lk = _rj(LOCK_FILE, None)
    return bool(lk) and time.time() - lk.get("beat", 0) < 30 * 60


def batch_status() -> dict:
    st = _rj(STATUS_FILE, {})
    st["running"] = batch_running()
    return st


def request_priority(index: str, date: str):
    q = _rj(PRIORITY_FILE, [])
    if [index, date] not in q:
        q.append([index, date])
    _wj(PRIORITY_FILE, q)


def backfill(start: str, end: str, indices=C.INDICES, log=print):
    days = [d.date().isoformat() for d in pd.bdate_range(start, end)][::-1]
    todo = [(i, d) for d in days for i in indices]
    skipped = {tuple(x) for x in _rj(SKIP_FILE, [])}
    builders = {i: Builder(i, log=log, fast=True) for i in indices}
    done = errors = streak = 0
    fails: dict = {}
    t0 = time.time()

    def status(cur=None, last_err=None):
        have = sum(1 for i, d in todo if read_meta(i, d))
        _wj(STATUS_FILE, {"from": start, "to": end, "total": len(todo) - len(skipped & set(todo)), "ready": have,
                          "current": cur, "errors": errors, "last_error": last_err,
                          "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t0)),
                          "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
        _wj(LOCK_FILE, {"pid": __import__("os").getpid(), "beat": time.time()})

    last_err = None
    while True:
        prio = [tuple(x) for x in _rj(PRIORITY_FILE, []) if not read_meta(*x)]
        _wj(PRIORITY_FILE, [list(x) for x in prio])
        nxt = prio[0] if prio else next(((i, d) for i, d in todo if (i, d) not in skipped and not read_meta(i, d)), None)
        if nxt is None:
            break
        i, d = nxt
        status(f"{i} {d}", last_err)
        try:
            b = builders.get(i) or Builder(i, log=log, fast=True)
            b.build(d)
            done += 1
            streak = 0
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"[:300]
            if "no " in msg and ("index data" in msg or "expiry found" in msg or "option bars" in msg):
                skipped.add((i, d))                      # holiday / nothing listed — never retry
                _wj(SKIP_FILE, sorted(list(x) for x in skipped))
                log(f"{i} {d}: skipped ({msg[:80]})")
            else:
                errors += 1
                streak += 1
                fails[(i, d)] = fails.get((i, d), 0) + 1
                last_err = f"{i} {d}: {msg}"
                log(f"ERROR {last_err}")
                if fails[(i, d)] >= 3:                   # this day keeps failing — set it aside, move on
                    skipped.add((i, d))
                    _wj(SKIP_FILE, sorted(list(x) for x in skipped))
                if streak >= 5:                          # probably throttled/blocked — back off
                    log("5 errors in a row — pausing 10 min")
                    for _ in range(10):
                        status("paused after errors", last_err)
                        time.sleep(60)
                    streak = 0
    status(None, last_err)
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass
    log(f"backfill finished: {done} days built, {errors} errors in {(time.time() - t0) / 3600:.1f} h")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", choices=list(C.INDICES))
    ap.add_argument("--date")
    ap.add_argument("--seconds", action="store_true", help="also fetch 1-second data for ATM±5")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--backfill", nargs=2, metavar=("FROM", "TO"), help="every weekday FROM..TO, both indices")
    a = ap.parse_args()
    if a.backfill:
        if batch_running():
            sys.exit("a backfill is already running")
        end = a.backfill[1]
        if end == "today":                     # the task runs after the close: include today
            end = (dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)).date().isoformat()
        backfill(a.backfill[0], end, [a.index] if a.index else C.INDICES)
        return
    if a.list:
        for p in list_packs(a.index):
            print(p)
        return
    m = Builder(a.index).build(a.date, seconds=a.seconds)
    print(json.dumps({k: m[k] for k in ("index", "date", "expiry", "atm_open")}), "| strikes", len(m["strikes"]),
          "| 1-second contracts", len(m["seconds"]))


if __name__ == "__main__":
    main()
