"""
sim_test.py — run the REAL engine over a historical day, second by second.
=========================================================================

replay.py proves the strategy core matches the backtest. This proves the parts
around it that only exist live: the tick -> minute-close builder, the 5-min
boundary timing, the paper fill path, restart-safe persistence and publishing.

A fake clock and a fake Kite replace the wall clock and the broker. Everything
else is engine.py unmodified. Per simulated minute m:

    m:00  option ticks at the minute's OPEN   (the backtest filled at option open)
    m:05  NIFTY tick at the minute's value, exchange-stamped inside minute m

so a minute's close is the last spot tick in it, exactly as it will be live.
Option intrabar highs are NOT emitted, so premium stops are checked on opens;
days with a backtest premium stop are reported separately (the live stop is
tick-based by design and books earlier, at the crossing price).

    python live_trading_options/nifty_pivot/sim_test.py 2026-06-08 2026-06-09
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import shutil
import tempfile
import datetime as dt
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT))

import engine as E                                                             # noqa: E402
from backtesting.options_pivot_intraday.breeze_adapter import BreezeOptionsLoader  # noqa: E402

RESULTS_BT = REPO / "backtesting" / "options_pivot_intraday" / "results"


class Clock:
    t = None


class FakeKWS:
    MODE_LTP, MODE_FULL = "ltp", "full"

    def subscribe(self, tokens):
        pass

    def set_mode(self, mode, tokens):
        pass


class FakeKite:
    def __init__(self, loader, day):
        self.loader, self.day = loader, day
        chain = loader.get_day_chain(day)
        self.expiry = pd.Timestamp(chain["expiry"].min()).date()
        self.rows, self.sym = [], {}
        tok = 900000
        for strike in sorted(chain["strike_price"].unique()):
            for opt, typ in (("PUT", "PE"), ("CALL", "CE")):
                tok += 1
                ts = f"NIFTYSIM{int(strike)}{typ}"
                self.rows.append({"name": "NIFTY", "instrument_type": typ, "strike": float(strike),
                                  "expiry": self.expiry, "instrument_token": tok,
                                  "tradingsymbol": ts, "lot_size": 75})
                self.sym[ts] = (int(strike), opt)

    def profile(self):
        return {"user_id": "SIM"}

    def instruments(self, exch):
        if exch == "NFO":
            return self.rows
        return [{"tradingsymbol": "NIFTY 50", "segment": "INDICES", "instrument_token": 256265}]

    def ltp(self, syms):
        out = {}
        op = self.loader.get_day_pivot(self.day, field="open")
        ts = pd.Timestamp(Clock.t).floor("min")
        for s in syms:
            key = self.sym.get(s.split(":", 1)[1])
            if key and key in op.columns and ts in op.index and pd.notna(op.at[ts, key]):
                out[s] = {"last_price": float(op.at[ts, key])}
        return out

    def historical_data(self, *a, **k):
        raise RuntimeError("sim: history API disabled (tests the cache path)")


def run_day(loader, day: str, tmp: Path, stop_at: dt.time = None) -> list[dict]:
    E.now_ist = lambda: Clock.t
    E.kx._instruments = lambda kite, exch: kite.instruments(exch)
    E.STATE_DIR = tmp / "live_state"
    E.RESULTS = tmp / "results"
    E.SEED_FILE = tmp / "seed.parquet"
    for d in (E.STATE_DIR, E.RESULTS):
        d.mkdir(parents=True, exist_ok=True)

    spot = loader.spot_1min_series()
    prior = spot[spot.index.normalize() < pd.Timestamp(day)]
    pd.DataFrame({"datetime": prior.index, "close": prior.values}).to_parquet(E.SEED_FILE, index=False)
    todays = spot[spot.index.normalize() == pd.Timestamp(day)]

    Clock.t = dt.datetime.combine(pd.Timestamp(day).date(), dt.time(9, 10))
    eng = E.Engine()
    eng._history_fyers = lambda start: (_ for _ in ()).throw(RuntimeError("sim: disabled"))
    eng.kite = FakeKite(loader, day)
    eng.kws = FakeKWS()
    eng.load_contracts()
    eng.seed()
    eng.restore_or_start()
    eng.subscribe(eng.band_tokens())

    op = loader.get_day_pivot(day, field="open")
    tok_of = {v: c["token"] for v, c in eng.contracts.items()}
    day0 = pd.Timestamp(day).date()

    for m_ts, value in todays.items():
        m = m_ts.to_pydatetime()
        if stop_at is not None and m.time() >= stop_at:
            eng.write_tick()
            eng.write_state()
            return eng.core.trades, eng
        # m:00 — option opens for this minute
        Clock.t = m
        if m_ts in op.index:
            row = op.loc[m_ts]
            ticks = [{"instrument_token": tok_of[k], "last_price": float(v)}
                     for k, v in row.items() if pd.notna(v) and k in tok_of]
            eng.on_ticks(None, ticks)
        eng.step()
        # m:05 — spot for this minute
        Clock.t = m + dt.timedelta(seconds=5)
        eng.on_ticks(None, [{"instrument_token": eng.spot_token, "last_price": float(value),
                             "exchange_timestamp": Clock.t}])
        for sec in (6, 30, 59):
            Clock.t = m + dt.timedelta(seconds=sec)
            eng.step()
    for extra in range(1, 4):
        Clock.t = dt.datetime.combine(day0, dt.time(15, 31)) + dt.timedelta(seconds=extra)
        eng.step()
    eng.write_tick()
    eng.write_state()
    eng._write_daily()
    return eng.core.trades, eng


def main(days):
    loader = BreezeOptionsLoader("NIFTY")
    bt = pd.read_csv(RESULTS_BT / "trades_breeze_oos.csv")
    bt["date"] = bt["date"].astype(str)
    all_ok = True
    for day in days:
        tmp = Path(tempfile.mkdtemp(prefix="ndp_sim_"))
        try:
            trades, eng = run_day(loader, day, tmp)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"{day}: ENGINE CRASHED — {e}")
            all_ok = False
            continue
        live = pd.DataFrame(trades)
        ref = bt[bt["date"] == day].copy()
        print(f"\n── {day} ({pd.Timestamp(day).day_name()}) — expiry {eng.expiry}, "
              f"seed: {eng.seed_source}, blocked: {eng.blocked_reason}")
        print(f"   levels R1 {eng.levels['r1']:.2f} S1 {eng.levels['s1']:.2f}")
        rows = []
        for i in range(max(len(live), len(ref))):
            a = live.iloc[i] if i < len(live) else None
            b = ref.iloc[i] if i < len(ref) else None
            rows.append({
                "live_entry": a["entry_time"][11:16] if a is not None else "-",
                "bt_entry": str(b["entry_time"])[11:16] if b is not None else "-",
                "side": (a["side"] if a is not None else b["side"]),
                "live_strike": int(a["strike"]) if a is not None else None,
                "bt_strike": int(b["strike"]) if b is not None else None,
                "live_px": f"{a['entry_price']:.2f}->{a['exit_price']:.2f}" if a is not None else "-",
                "bt_px": f"{b['entry_price']:.2f}->{b['exit_price']:.2f}" if b is not None else "-",
                "live_exit": f"{a['exit_time'][11:16]} {a['exit_reason']}" if a is not None else "-",
                "bt_exit": f"{str(b['exit_time'])[11:16]} {b['exit_reason']}" if b is not None else "-",
            })
        print(pd.DataFrame(rows).to_string(index=False))
        stop_day = ref["exit_reason"].eq("premium_stop").any()
        same = (len(live) == len(ref) and all(
            r["live_entry"] == r["bt_entry"] and r["live_strike"] == r["bt_strike"]
            and r["live_px"] == r["bt_px"] and r["live_exit"] == r["bt_exit"] for r in rows))
        files = sorted(p.name for p in (tmp / "live_state").iterdir()) + \
            sorted(p.name for p in (tmp / "results").iterdir())
        st = json.loads((tmp / "live_state" / f"{day}_STATE.json").read_text())
        print(f"   published: {files}")
        print(f"   state: {len(st['candles'])} candles, {len(st['markers'])} markers, "
              f"{len(st['events'])} events, day net Rs {st['day']['day_net']:,.0f}")
        verdict = "IDENTICAL to backtest" if same else (
            "differs (expected: backtest premium stop uses 1-min highs)" if stop_day else "DIFFERS")
        print(f"   RESULT: {verdict}")
        all_ok &= same or stop_day
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nOVERALL: {'PASS' if all_ok else 'FAIL'}")
    return all_ok


if __name__ == "__main__":
    days = sys.argv[1:] or ["2026-06-08", "2026-06-09", "2026-06-12", "2026-07-02", "2026-06-23"]
    raise SystemExit(0 if main(days) else 1)
