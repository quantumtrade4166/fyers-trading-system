"""
cepe_imbalance/fix_loop_day.py — rebuild a PAPER book's day after the 17-Nov adjustment loop.
=============================================================================================

Owner 17-Nov-2026: "correct today's crude P&L in both books using the replay and make them trade seamlessly
like nothing happened". The C -> ITM back -> B cycle (fixed by the loop guard in strategy.py) made 60,133
fills on CRUDEOIL and 247,801 on CRUDEOIL_ST from 17:28:02.

How (the PAPER engine must be STOPPED while this runs — it rewrites the book files):
  1. everything BEFORE the cut-off (first loop fill) is kept exactly: the real fills are re-applied at their
     real prices and times, the real events and chart rows are kept;
  2. from the cut-off the real strategy code (with the loop guard) trades the recorded quotes
     (data/quotes/{date}/{und}.qrec) under a replay clock, up to the last recorded tick;
     Supertrend books get the same signal as live (previous-session warm-up + 0.5 s ATM CE+PE samples);
  3. the book, fills, events, state and chart rows are written back; the originals are moved to
     data/books/{date}/_loop_backup/ and data/history/{date}/_loop_backup/ first.
On start the engine loads the corrected book and carries on trading the live market from there.

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.fix_loop_day CRUDEOIL 2026-11-17 17:28:02
"""
import argparse
import datetime as dt
import json
import shutil
import sys
import types
from dataclasses import asdict

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import quotes as Q
from live_trading_options.cepe_imbalance import replay_rules as RR
from live_trading_options.cepe_imbalance import st_signal as STS
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _jsonl(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def rebuild(name, date, cut_hms, write=False):
    und = C.und_of(name)
    books, hist = C.DATA / "books" / date, C.DATA / "history" / date
    real_trades = _jsonl(books / f"{name}_trades.jsonl")
    real_events = _jsonl(books / f"{name}_events.jsonl")
    real_hist = _jsonl(hist / f"{name}.jsonl")
    real_state = json.loads((books / f"{name}_state.json").read_text(encoding="utf-8"))
    day = dt.date.fromisoformat(date)
    cut = dt.datetime.combine(day, dt.time.fromisoformat(cut_hms), IST).timestamp()
    keep_trades = [t for t in real_trades if t["ts"] < cut]
    keep_events = [e for e in real_events if e["ts"] < cut]
    keep_hist = [h for h in real_hist if h["t"] < cut]

    RR.install_clock()
    u = RR.universe_for(und, date)
    m = Market(u)
    o, close, sq = EN.session(und, day)
    r = EN.Runner(name, m, persist=False, date=date)
    r.params.update(real_state.get("params") or {})
    st = r.strategy
    events = []
    orig_event = r.event

    def event(ev, **f):
        orig_event(ev, **f)
        events.append(r.events[-1])
    r.event = event
    variant = C.is_variant(name)
    step = float(r.params.get("step") or C.STRIKE_STEP.get(und) or 0)
    buf = float(r.params.get("atm_buffer_pct", 20.0))
    sig = STS.StSignal(und) if variant else None
    if variant:
        r.ctx.signal = lambda per, mul: sig.state(per, mul)
    sp_host = types.SimpleNamespace(market=m)

    def hist_row(t):
        mt, im = r.book.mtm(), r.book.imbalance()
        e = m.expiry(und, r.expiry_n)
        atm = r.state.get("atm") or m.atm(und, e)
        row = {"t": round(t, 2), "mtm": mt["total"], "imb": im["pct"], "ce": im["ce_value"], "pe": im["pe_value"],
               "ref": m.ref_price(und, e), "atm": atm}
        if variant:
            s = r.state.get("st") or {}
            atm = s.get("atm") or atm
            row["atm"] = atm
            if s.get("st") is not None:
                row["st"], row["st_red"] = s["st"], s.get("trend") == "red"
        if atm and e:
            for kind, f in (("CE", "ace"), ("PE", "ape")):
                c = m.contract(und, e, atm, kind)
                px = m.mark(c.key) if c else 0.0
                row[f] = px if px > 0 else None
        sp = EN.Engine.spreads(sp_host, und, r, e, atm)
        if sp:
            row["sp"] = sp
        return row

    ti = 0
    expiry = None
    seeded = False
    new_hist = []
    next_eval = next_sig = next_row = 0.0
    prev = None
    last_t = None
    for _m, row in Q.iter_window(date, und, o.timestamp() - 600, sq.timestamp() + 1):
        t = row[0]
        if prev is not None and t != prev:
            RR.Clock.t = prev
            if variant and expiry is None and prev >= o.timestamp():
                expiry = m.expiry(und, 0)
                sig.reset(date, expiry)
                warm, today_b, src, wd = STS.warm_up(u, und, date, expiry, step, buf, EN.session,
                                                     now_ts=o.timestamp())
                sig.install_warmup(warm, today_b, src, wd)
            if variant and expiry and prev >= next_sig:
                sig.sample(prev, m, step, buf, o.timestamp(), close.timestamp())
                next_sig = prev + STS.SAMPLE_S
            while ti < len(keep_trades) and keep_trades[ti]["ts"] <= prev:       # the real fills, as they were
                tr = keep_trades[ti]
                c = u.by_key[tr["key"]]
                RR.Clock.t = tr["ts"]
                r.book.order(c, tr["side"], tr["lots"], tr["reason"], price=tr["price"])
                ti += 1
            RR.Clock.t = prev
            if prev >= cut:
                if not seeded:                                                    # hand over to the rules
                    has = any(p.lots for p in r.book.positions.values())
                    r.state = {k: v for k, v in (real_state.get("state") or {}).items()
                               if k not in ("book_hist", "adj_minute", "loop_logged", "blocked", "blocked_key",
                                            "blocked_keys")}
                    if keep_hist and keep_hist[-1].get("atm"):
                        r.state["atm"] = keep_hist[-1]["atm"]
                    r.state["phase"] = "in" if has else ("flat" if variant else "wait_entry")
                    r.running = True
                    seeded = True
                    next_eval = prev
                if prev >= next_eval:
                    st.on_tick(r.ctx, [])
                    next_eval = prev + RR.EVAL_EVERY_S
                if prev >= next_row:
                    new_hist.append(hist_row(prev))
                    next_row = prev + C.HISTORY_EVERY_S
        if t >= sq.timestamp():
            break
        RR.Clock.t = t
        m.apply([Tick(row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], int(t), t)])
        prev = last_t = t
    new_trades = r.book.trades[len(keep_trades):]
    pos = r.book.positions
    res = {"name": name, "cut": cut_hms, "kept_fills": len(keep_trades), "new_fills": len(new_trades),
           "real_fills": len(real_trades), "last_tick": dt.datetime.fromtimestamp(last_t, IST).strftime("%H:%M:%S"),
           "mtm": round(r.book.mtm()["total"], 2), "real_mtm_now": real_hist[-1]["mtm"] if real_hist else None,
           "open": {p.symbol: p.lots for p in pos.values() if p.lots}, "phase": r.state.get("phase"),
           "loop_guard": sum(1 for e in events if e["event"] == "loop_guard"), "errors": r.errors,
           "last_error": r.last_error}
    if write:
        for folder, files in ((books, (f"{name}.json", f"{name}_trades.jsonl", f"{name}_events.jsonl",
                                      f"{name}_state.json")), (hist, (f"{name}.jsonl", f"{name}.stats.json"))):
            bk = folder / "_loop_backup"
            bk.mkdir(exist_ok=True)
            for f in files:
                if (folder / f).exists() and not (bk / f).exists():
                    shutil.copy2(folder / f, bk / f)
        (books / f"{name}.json").write_text(json.dumps(
            {"name": name, "date": date, "saved_at": RR.Clock.t, "corrected": f"loop fix from {cut_hms}",
             "positions": {k: asdict(v) for k, v in pos.items()}}, indent=1), encoding="utf-8")
        (books / f"{name}_trades.jsonl").write_text("".join(json.dumps(t) + "\n" for t in r.book.trades),
                                                   encoding="utf-8")
        note = {"ts": cut, "time": cut_hms, "event": "loop_fix",
                "note": f"17-Nov adjustment loop removed: {len(real_trades) - len(keep_trades):,} loop fills "
                        f"replaced by {len(new_trades)} fills replayed on recorded ticks with the loop guard"}
        (books / f"{name}_events.jsonl").write_text(
            "".join(json.dumps(e, default=str) + "\n" for e in keep_events + [note] + events), encoding="utf-8")
        state = dict(real_state)
        state.update({"running": True, "square_off_done": False, "state": r.state})
        (books / f"{name}_state.json").write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
        (hist / f"{name}.jsonl").write_text("".join(json.dumps(h) + "\n" for h in keep_hist + new_hist),
                                           encoding="utf-8")
        (hist / f"{name}.stats.json").unlink(missing_ok=True)
        res["written"] = True
    return res


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("date")
    ap.add_argument("cut", help="HH:MM:SS - first loop fill; everything before is kept as it was")
    ap.add_argument("--write", action="store_true", help="rewrite the book files (engine STOPPED)")
    a = ap.parse_args()
    print(json.dumps(rebuild(a.name, a.date, a.cut, a.write), indent=1))
