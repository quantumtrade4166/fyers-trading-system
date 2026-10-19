"""Replay real days through the LIVE engine and compare with the backtest.

Each 1-minute Breeze bar becomes four ticks (open at :00, then high/low, close at
:45) for NIFTY and every contract the backtest touched that day. The engine runs
on a simulated clock: its own minute books, 5-min candles, Supertrend, boundary
timing (T + 1.5 s) and LTP paper fills. Kite/Fyers are replaced by Breeze bars;
nothing else is stubbed.

    .venv\\Scripts\\python.exe -m live_trading_options.nifty_st_directional.sim_test --days 2026-09-01 2026-09-03
    .venv\\Scripts\\python.exe -m live_trading_options.nifty_st_directional.sim_test --start 2026-08-10 --end 2026-10-09

PASS = identical trades (contract, entry/exit time and price, exit reason).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import datetime as dt
from pathlib import Path

import pandas as pd

from backtesting.options_st_directional.config import Config
from backtesting.options_st_directional.data import BreezeProvider
from backtesting.options_st_directional.engine import run_day
import live_trading_options.nifty_st_directional.engine as LE

# never touch the real paper book: every file the engine writes goes to sim_out/
SIM_OUT = Path(__file__).resolve().parent / "data" / "sim_out"


def use_out_dir(path: Path):
    LE.STATE_DIR = path / "live_state"
    LE.RESULTS = path / "results"
    for d in (LE.STATE_DIR, LE.RESULTS):
        d.mkdir(parents=True, exist_ok=True)


def ticks_of(bars: pd.DataFrame):
    """(time, price) ticks reproducing each 1-min bar's OHLC."""
    out = []
    for r in bars.itertuples():
        m = pd.Timestamp(r.datetime)
        if m.strftime("%H:%M") >= "15:30":
            continue
        first, second = (r.high, r.low) if r.close < r.open else (r.low, r.high)
        out += [(m, r.open), (m + pd.Timedelta(seconds=15), first),
                (m + pd.Timedelta(seconds=30), second), (m + pd.Timedelta(seconds=45), r.close)]
    return out


def sim_day(day: str, bp: BreezeProvider, cfg: Config):
    bt = run_day(day, bp, cfg)
    contracts = {s.contract for s in bt["setups"]}

    clock = {"now": pd.Timestamp(f"{day} 09:05").to_pydatetime()}
    eng = LE.Engine(clock=lambda: clock["now"])
    # core rules only: the live stop-to-cost works on ticks, the backtest's on 1-min
    # bars, so they are compared separately (see test below / --breakeven backtests)
    eng.p = dict(eng.p, breakeven_after_points=None, cpr_max_width=None)
    eng.cfg.cpr_max_width = None
    eng.blocked_reason = None
    sel = bp.select_contract(day, None, 25000)
    eng.trade_expiry = sel["expiry"].date()
    eng.expiry_reason = sel["reason"]
    eng.lot_size = 65
    eng.qty = eng.lot_size * eng.p["lots"]
    eng.spot_token = 1
    tok = 100
    for (exp, k, opt) in contracts:
        key = (pd.Timestamp(exp).date(), int(k), opt)
        eng.instr[key] = {"token": tok, "tradingsymbol": f"SIM{k}{opt}", "lot_size": 65}
        eng.by_token[tok] = key
        tok += 1

    prev = bp.prev_day(day)
    spot1 = bp.spot1

    def history(symbol, kite_token, sessions):
        if kite_token == eng.spot_token:
            days = [d for d in bp.trading_days if d < day][-sessions:]
            h = spot1[spot1["datetime"].dt.strftime("%Y-%m-%d").isin(days)]
            return h[["datetime", "open", "high", "low", "close"]]
        exp, k, opt = eng.by_token[kite_token]
        c = (pd.Timestamp(exp), k, opt)
        frames = [bp._bars(d, c) for d in [x for x in bp.trading_days if x < day][-sessions:]]
        h = pd.concat(frames) if frames else pd.DataFrame()
        return h[["datetime", "open", "high", "low", "close"]] if not h.empty else h

    eng.history = history
    eng.kws = None
    eng.subscribed = set()

    def subscribe(tokens):          # no socket: just mark the subscription time
        for t in tokens:
            if t in eng.subscribed:
                continue
            eng.subscribed.add(t)
            book = eng.spot_book if t == eng.spot_token else eng.books.setdefault(t, LE.MinuteBook())
            if book.sub_at is None:
                book.sub_at = clock["now"]
    eng.subscribe = subscribe

    eng.seed_spot()
    subscribe([eng.spot_token] + list(eng.by_token))
    for key in eng.instr:
        eng.ensure_tracked(key)

    # every tick of the day, plus a decision point 1.5 s after each boundary
    feed = [(t, eng.spot_token, p) for t, p in
            ticks_of(spot1[spot1["datetime"].dt.strftime("%Y-%m-%d") == day])]
    for tk, key in eng.by_token.items():
        c = (pd.Timestamp(key[0]), key[1], key[2])
        feed += [(t, tk, p) for t, p in ticks_of(bp._bars(day, c))]
    feed.sort(key=lambda x: (x[0], x[1]))
    steps = [pd.Timestamp(f"{day} 09:20") + pd.Timedelta(minutes=5 * i, seconds=2) for i in range(72)]
    i = 0
    for st in steps:
        while i < len(feed) and feed[i][0] <= st:
            t, tk, p = feed[i]
            clock["now"] = t.to_pydatetime()
            with eng.lock:
                eng.tick(tk, float(p), clock["now"])
            i += 1
        clock["now"] = st.to_pydatetime()
        eng.first_tick_seen = True
        eng.step()
        eng._last_mtm = 0.0
        eng.write_tick()            # MTM samples for the chart (sim clock)
        if eng.machine.done:
            break
    clock["now"] = pd.Timestamp(f"{day} 15:16:05").to_pydatetime()
    eng.step()
    eng.status = "closed (simulated)"
    eng._write_daily()
    eng.write_state()

    live = eng.machine.result()["trades"]
    cols = ["opt_type", "strike", "entry_ts", "entry_price", "exit_ts", "exit_price", "exit_reason"]
    norm = lambda trs: [tuple(str(pd.Timestamp(t[c]).floor("min")) if c.endswith("_ts") else
                              (round(float(t[c]), 2) if "price" in c else t[c]) for c in cols) for t in trs]
    a, b = norm(bt["trades"]), norm(live)
    return a == b, a, b, eng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--out", default=str(SIM_OUT), help="where the simulated engine writes")
    a = ap.parse_args()
    use_out_dir(Path(a.out))
    cfg = Config()
    bp = BreezeProvider(cfg, verbose=False)
    days = a.days or bp.days_in_range(a.start, a.end)
    ok_all = True
    for d in days:
        ok, bt, lv, _ = sim_day(d, bp, cfg)
        ok_all &= ok
        print(f"{d}: {'PASS' if ok else 'FAIL'}  backtest {len(bt)} trade(s), live {len(lv)}")
        if not ok:
            for x in bt:
                print("   bt  ", x)
            for x in lv:
                print("   live", x)
    print("ALL PASS" if ok_all else "MISMATCHES FOUND")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
