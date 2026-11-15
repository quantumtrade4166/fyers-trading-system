"""
cepe_imbalance/replay_rules.py — replay a recorded day tick by tick through the REAL CE/PE strategy code.
=======================================================================================================

Offline what-if tool (owner 15-Nov-2026, after the BTC 14-Nov loss). Nothing here touches the live engine.

  * ticks   data/quotes/{date}/{und}.qrec (every tick the engine recorded), applied in order
  * clock   a fake clock follows the tick time, so session / square-off / staleness rules see replay time
  * rules   "live"  = CePeImbalance exactly as running
            "owner" = owner's 15-Nov test rules (CePeOwnerTest below)
  * extras  cooldown_s (seconds between adjustments), square-off time override

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.replay_rules BTC 2026-11-14 --start 11:40:55
"""
import argparse
import datetime as dt
import json
import sys
import types

from live_trading_options.cepe_imbalance import book as BK
from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import instruments as I
from live_trading_options.cepe_imbalance import quotes as Q
from live_trading_options.cepe_imbalance import strategy as S
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
EVAL_EVERY_S = 0.2                    # live evaluates once per feed batch; a batch is ~0.1-0.5 s of ticks


# ── fake clock ──────────────────────────────────────────────────────────────
class Clock:
    t = 0.0


class _FakeDatetime(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return dt.datetime.fromtimestamp(Clock.t, tz)


_fake_dt = types.SimpleNamespace(datetime=_FakeDatetime, date=dt.date, time=dt.time, timedelta=dt.timedelta,
                                 timezone=dt.timezone)
_fake_time = types.SimpleNamespace(time=lambda: Clock.t, sleep=lambda s: None, perf_counter=lambda: Clock.t)


def install_clock():
    for mod in (EN, S, BK, I):
        if hasattr(mod, "dt"):
            mod.dt = _fake_dt
        if hasattr(mod, "time"):
            mod.time = _fake_time


# ── owner's 15-Nov rules now live in strategy.CePeNewRules (paper book BTC_NR) ──
CePeOwnerTest = S.CePeNewRules


RULES = {"live": S.CePeImbalance, "owner": CePeOwnerTest}


# ── replay ──────────────────────────────────────────────────────────────────
def universe_for(und, date, products_file=None):
    """Kotak instruments from the engine's scrip cache; BTC from a saved Delta products file (default: the
    newest data/delta_products_*.json, else testdata/delta_products_sample.json)."""
    from live_trading_options.cepe_imbalance import crypto
    texts = {}
    for seg in C.KOTAK_SEGS:
        p = C.DATA / f"scrip_{seg}_{date}.csv"
        if p.exists():
            texts[seg] = p.read_text(encoding="utf-8")
    u = I.build(texts, today=date)
    if C.kind_of(und) == "crypto":
        if products_file:
            prods = json.loads(open(products_file, encoding="utf-8").read())
        else:
            prods = crypto.cached_products() or json.loads(
                (C.HERE / "testdata" / "delta_products_sample.json").read_text(encoding="utf-8"))
        crypto.attach(u, prods, keep_settled=True)
    return u


def run(und, date, start, rules="live", params=None, cooldown_s=0.0, square_off=None, folder=None,
        universe=None, rows=None, checkpoints=()):
    """-> dict with final P&L, fills, adjustment counts, MTM at checkpoints ("HH:MM")."""
    install_clock()
    u = universe or universe_for(und, date)
    m = Market(u)
    day = dt.date.fromisoformat(date)
    o, close, sq = EN.session(und, day)
    if square_off:
        sq = dt.datetime.combine(day, dt.time.fromisoformat(square_off), IST)
    t0 = dt.datetime.combine(day, dt.time.fromisoformat(start), IST).timestamp() if start else o.timestamp()
    Clock.t = t0 - 600
    r = EN.Runner(und, m, persist=False, date=date)
    r.strategy = RULES[rules]()
    r.params = dict(S.CePeImbalance.params)
    r.params.update(params or {})
    st = r.strategy
    last_adj = [-1e18]
    if cooldown_s:                                         # wrap execute: no adjustment within cooldown
        orig = st.execute

        def execute(ctx, ops, reason):
            if not reason.startswith("entry") and Clock.t - last_adj[0] < cooldown_s:
                st._fail = "cooldown"
                return False
            ok = orig(ctx, ops, reason)
            if ok:
                last_adj[0] = Clock.t
            return ok
        st.execute = execute
    started = False
    next_eval = 0.0
    cps = sorted((dt.datetime.combine(day, dt.time.fromisoformat(c), IST).timestamp(), c) for c in checkpoints)
    marks, ci = {}, 0
    it = rows if rows is not None else (row for _m, row in Q.iter_window(date, und, t0 - 600, sq.timestamp() + 1,
                                                                          folder=folder))
    prev = None
    for row in it:
        t = row[0]
        # evaluate when time ADVANCES, so a snapshot (many rows with one time) is applied whole first
        if prev is not None and t != prev and prev >= t0:
            Clock.t = prev
            if not started:
                r.running = True
                st.on_start(r.ctx)
                started = True
                next_eval = prev + EVAL_EVERY_S
            elif prev >= next_eval:
                st.on_tick(r.ctx, [])
                next_eval = prev + EVAL_EVERY_S
        if t >= sq.timestamp():
            break
        Clock.t = t
        while ci < len(cps) and t >= cps[ci][0]:
            marks[cps[ci][1]] = r.book.mtm()["total"]
            ci += 1
        m.apply([Tick(row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], int(t), t)])
        prev = t
    Clock.t = sq.timestamp()
    before = r.book.mtm()["total"]
    r.book.exit_all("square-off")
    for _t, c in cps[ci:]:
        marks.setdefault(c, before)
    adj = {}
    for e in r.events:
        if e.get("event") == "adjust":
            k = e["action"].split(" (")[0]
            adj[k] = adj.get(k, 0) + 1
    crypto_ = C.kind_of(und) == "crypto"
    half = sum((t["ask"] - t["bid"]) / 2 * t["lots"] * C.DELTA_LOT * 0.001 for t in r.book.trades
               if t.get("bid") and t.get("ask")) if crypto_ else None
    # Delta fee per side: lower of 0.01% of notional and 3.5% of premium (strike stands in for spot)
    fees = sum(min(0.0001 * t["strike"], 0.035 * t["price"]) * t["lots"] * C.DELTA_LOT * 0.001
               for t in r.book.trades) if crypto_ else None
    return {"rules": rules, "params": params or {}, "cooldown_s": cooldown_s, "square_off": sq.strftime("%H:%M"),
            "pnl": round(r.book.mtm()["total"], 2), "pnl_before_exit": round(before, 2),
            "fees": round(fees, 2) if fees is not None else None,
            "net": round(r.book.mtm()["total"] - (fees or 0), 2), "entered": started,
            "fills": len(r.book.trades), "adjust": adj, "half_spread": round(half, 2) if half is not None else None,
            "marks": marks, "errors": r.errors, "last_error": r.last_error}


def load_rows(und, date, folder=None):
    """All of one day's rows in memory (the grid reuses them)."""
    return [row for _m, row in Q.iter_window(date, und, 0, 2e10, folder=folder)]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("und")
    ap.add_argument("date")
    ap.add_argument("--start", default="")
    ap.add_argument("--rules", default="live")
    ap.add_argument("--band", type=float, default=15)
    ap.add_argument("--step", type=float, default=0)
    ap.add_argument("--cooldown", type=float, default=0)
    ap.add_argument("--square-off", default="")
    ap.add_argument("--folder", default="")
    a = ap.parse_args()
    from pathlib import Path
    res = run(a.und, a.date, a.start, a.rules, {"band": a.band, "step": a.step}, a.cooldown, a.square_off or None,
              Path(a.folder) if a.folder else None, checkpoints=("13:00", "15:00", "16:30", "17:00"))
    print(json.dumps(res, indent=1))
