"""
cepe_imbalance/replay_syn.py — replay recorded NIFTY / SENSEX days through the REAL Synthetic To-and-Fro rules.
=============================================================================================================

Owner 23-Nov-2026: "backtest this on the last 4/5 days of NIFTY we have recorded ticks".
  * ticks   data/quotes/{date}/{und}.qrec (every tick the paper engine recorded), applied in time order
  * clock   fake clock = tick time (entry time, exit time, staleness all see replay time)
  * fills   PaperBook: SELL at the bid, BUY at the ask (spread paid on every order)
  * costs   statutory charges + Rs 20 / order brokerage (backtesting.options_pivot_intraday.costs)

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.replay_syn NIFTY 2026-11-16 2026-11-17 ...
    options: --lots 1 --buffer 5 --entry 09:20 --exit 15:15 --csv out.csv
"""
import argparse
import datetime as dt
import json
import sys

from live_trading_options.cepe_imbalance import config as C
from live_trading_options.cepe_imbalance import engine as EN
from live_trading_options.cepe_imbalance import quotes as Q
from live_trading_options.cepe_imbalance import replay_rules as RR
from live_trading_options.cepe_imbalance import synthetic as SY
from live_trading_options.cepe_imbalance.feed import Tick
from live_trading_options.cepe_imbalance.market import Market

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def charges(trades, lot, date):
    try:
        from backtesting.options_pivot_intraday.costs import statutory_costs
    except Exception:
        return None
    by = {}
    for t in trades:
        v = by.setdefault(t["symbol"], {"S": [0, 0.0, 0], "B": [0, 0.0, 0]})
        k = "S" if t["side"] == "SELL" else "B"
        u = int(t["lots"]) * lot
        v[k][0] += u
        v[k][1] += u * float(t["price"])
        v[k][2] += 1
    est = 0.0
    for v in by.values():
        q = max(v["S"][0], v["B"][0])
        if not q:
            continue
        sp = v["S"][1] / v["S"][0] if v["S"][0] else 0.0
        bp = v["B"][1] / v["B"][0] if v["B"][0] else 0.0
        est += statutory_costs(sp, bp, q, date)["total"] + 20.0 * max(0, v["S"][2] + v["B"][2] - 2)
    return round(est, 2)


LOT_FALLBACK = {"NIFTY": 65, "SENSEX": 20}
PATH_EVERY_S = 15                  # MTM path sample (summary 'path')
CHAIN_EACH_SIDE = 8                # replay player option chain: strikes each side of ATM


def meta_of(und, date, folder=None) -> dict:
    """{key: [symbol, kind, expiry, strike]} of every contract in the day's recording (meta only, rows not parsed)."""
    import struct
    import zlib
    p = (folder or (C.DATA / "quotes")) / date / f"{und}.qrec"
    meta = {}
    with open(p, "rb") as f:
        while True:
            head = f.read(4)
            if len(head) < 4:
                break
            n = struct.unpack("<I", head)[0]
            z = f.read(n)
            if len(z) < n:
                break
            txt = zlib.decompress(z).decode()
            i = txt.find(',"rows":')
            if txt.startswith('{"meta":') and i > 0:
                meta.update(json.loads(txt[8:i]))
    return meta


def universe_from_recording(und, date, folder=None):
    """The scrip cache only keeps today's file, so old days (expired weeklies) are rebuilt from the recording's
    own contract list. Lot / tick from today's scrip file (same month), else the fallback."""
    from live_trading_options.cepe_imbalance import instruments as I
    lot = None
    try:
        today = max(p.stem.rsplit("_", 1)[-1] for p in C.DATA.glob(f"scrip_{C.UNDERLYINGS[und]['seg']}_*.csv"))
        lot = RR.universe_for(und, today).underlyings[und].lot or None
    except Exception:
        pass
    lot = lot or LOT_FALLBACK[und]
    spec = C.UNDERLYINGS[und]
    U = I.Underlying(name=und, seg=spec["seg"], spot_key=spec["spot"], lot=lot, mult=1.0)
    by_key = {}
    for key, (sym, kind, exp, strike) in meta_of(und, date, folder).items():
        if kind not in ("CE", "PE") or not exp or exp < date:
            continue
        seg, tok = key.split("|", 1)
        c = I.Contract(key=key, seg=seg, token=int(tok) if tok.isdigit() else 0, symbol=sym, name=und, kind=kind,
                       expiry=exp, strike=float(strike), lot=lot, tick=0.05, mult=1.0)
        by_key[key] = c
        U.chains.setdefault(exp, {}).setdefault(float(strike), {})[kind] = c
    for e in list(U.chains):
        U.chains[e] = {k: v for k, v in sorted(U.chains[e].items()) if "CE" in v and "PE" in v}
    unds = {n: I.Underlying(name=n, seg=s["seg"], spot_key=s["spot"]) for n, s in C.UNDERLYINGS.items()}
    unds[und] = U
    return I.Universe(underlyings=unds, by_key=by_key, file_date=date)


def snapshot(r, m, und, t):
    """One per-second frame for the replay player: prices, MTM, imbalance, held legs, trigger levels."""
    mt, im = r.book.mtm(), r.book.imbalance()
    legs = []
    for p in r.book.positions.values():
        if p.lots:
            q = m.quote(p.key)
            ltp = m.mark(p.key)
            legs.append([p.kind, p.strike, p.lots, round(p.avg, 2), round(ltp or 0, 2),
                         round(p.unrealized(ltp), 2), q.bid if q else None, q.ask if q else None])
    legs.sort(key=lambda x: (x[0], -x[1]))
    lv = r.strategy.levels(r.ctx) if hasattr(r.strategy, "levels") else None
    # option chain around ATM (strategy grid, CHAIN_EACH_SIDE strikes each side): [strike, CE, PE] at mark price
    chain, booked = [], []
    step = r.strategy.step(r.ctx)
    c0 = r.state.get("atm") or (m.atm(und, r.ctx.expiry) if r.ctx.expiry else None)
    if c0:
        for i in range(-CHAIN_EACH_SIDE, CHAIN_EACH_SIDE + 1):
            k = c0 + i * step
            px = []
            for kind in ("CE", "PE"):
                c = r.ctx.contract(k, kind)
                v = m.mark(c.key) if c else 0
                px.append(round(v, 2) if v else None)
            chain.append([k, px[0], px[1]])
    for p in r.book.positions.values():                       # booked (closed) P&L per strike
        if p.realized:
            booked.append([p.kind, p.strike, round(p.realized, 2)])
    return {"t": round(t, 1), "syn": r.state.get("syn"), "chain": chain, "booked": booked, "spot": m.ref_price(und, r.ctx.expiry), "atm": r.state.get("atm"),
            "mtm": mt["total"], "real": mt["realized"], "imb": im["pct"], "ce": im["ce_value"], "pe": im["pe_value"],
            "legs": legs, "lv": lv}


def run_day(und, date, lots=1, buffer=5.0, entry="09:20", exit_="15:15", folder=None, rules="syn", params=None, timeline=None):
    """rules "syn" = Synthetic To-and-Fro (book {UND}_SYN) · "cepe" = the CE/PE imbalance rules the {UND} paper
    book trades (flex for NIFTY / SENSEX), with that book's saved settings unless `params` overrides them."""
    RR.install_clock()
    SY.time = RR._fake_time
    u = universe_from_recording(und, date, folder)
    m = Market(u)
    day = dt.date.fromisoformat(date)
    t0 = dt.datetime.combine(day, dt.time.fromisoformat(entry), IST).timestamp()
    t1 = dt.datetime.combine(day, dt.time.fromisoformat(exit_), IST).timestamp()
    RR.Clock.t = t0 - 600
    r = EN.Runner(und + (C.SYN_SUFFIX if rules == "syn" else ""), m, persist=False, date=date)
    st = r.strategy
    r.ctx.in_session = lambda now=None: True
    if rules == "syn":
        assert isinstance(st, SY.SynToFro), r.strategy_name
        r.params.update(lots=lots, buffer=buffer, exit_time="")
    else:
        saved = (EN.load_settings(und).get(und) or {}).get("params") or {}
        r.params.update({k: v for k, v in saved.items() if k in r.params})
        r.params.update(lots=lots, exit_above=0.0, exit_below=0.0, mtm_stop=0.0, profit_target=0.0, lock_start=0.0,
                        lock_gap=0.0)                     # like-for-like with the SYN replay: no money exits
    r.params.update(params or {})
    lot = u.underlyings[und].lot
    started, prev, next_eval = False, None, 0.0
    lo, hi, lo_t, path = 0.0, 0.0, None, []
    syn0 = None
    for _m, row in Q.iter_window(date, und, t0 - 900, t1 + 1, folder=folder):
        t = row[0]
        if prev is not None and t != prev and prev >= t0:
            RR.Clock.t = prev
            if not started:
                r.running = True
                st.on_start(r.ctx)
                started = r.state.get("phase") == "in" or started
                syn0 = r.state.get("syn")
                next_eval = prev + RR.EVAL_EVERY_S
            elif prev >= next_eval:
                if r.state.get("phase") == "wait_entry":
                    st.on_start(r.ctx)
                else:
                    st.on_tick(r.ctx, [])
                next_eval = prev + RR.EVAL_EVERY_S
                mt = r.book.mtm()["total"]
                if mt < lo:
                    lo, lo_t = mt, dt.datetime.fromtimestamp(prev, IST).strftime("%H:%M")
                hi = max(hi, mt)
                if not path or prev - path[-1][0] >= PATH_EVERY_S:
                    path.append((prev, mt, r.state.get("syn")))
                if timeline is not None and (not timeline or prev - timeline[-1]["t"] >= 1.0):
                    timeline.append(snapshot(r, m, und, prev))
        if t >= t1:
            break
        RR.Clock.t = t
        m.apply([Tick(row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], int(t), t)])
        prev = t
    RR.Clock.t = t1
    syn1 = r.state.get("syn")
    r.book.exit_all("exit time")
    adj = {}
    for e in r.events:
        if e.get("event") == "adjust" and not e["action"].startswith("entry"):
            k = e["action"].split(" (")[0]
            adj[k] = adj.get(k, 0) + 1
    gross = round(r.book.mtm()["total"], 2)
    ch = charges(r.book.trades, lot, date)
    entry_ev = next((e for e in r.events if e.get("event") == "adjust" and e["action"].startswith("entry")), None)
    if timeline is not None:
        timeline.append(snapshot(r, m, und, t1))
        timeline.append({"events": [e for e in r.events if e.get("event") in ("adjust", "exit_time", "stop_loss", "blocked")],
                         "trades": r.book.trades, "expiry": r.ctx.expiry})
    return {"und": und, "date": date, "rules": r.strategy_name, "lots": lots, "lot": lot, "entered": bool(entry_ev),
            "entry_time": entry_ev["time"] if entry_ev else None, "syn_start": syn0, "syn_end": syn1,
            "fills": len(r.book.trades), "adjustments": sum(adj.values()), "by_type": adj,
            "gross": gross, "charges": ch, "net": round(gross - (ch or 0), 2),
            "mtm_low": round(lo, 2), "mtm_low_at": lo_t, "mtm_high": round(hi, 2),
            "errors": r.errors, "last_error": r.last_error,
            "params": {k: r.params.get(k) for k in ("band", "normal_band", "normal_trigger", "atm_buffer_pct", "buffer", "mtm_stop", "profit_target", "lock_start", "lock_gap") if k in r.params},
            "path": [(dt.datetime.fromtimestamp(a, IST).strftime("%H:%M"), round(b, 2), c) for a, b, c in path]}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("und")
    ap.add_argument("dates", nargs="+")
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--buffer", type=float, default=5.0)
    ap.add_argument("--entry", default="09:20")
    ap.add_argument("--exit", default="15:15")
    ap.add_argument("--json", default="")
    ap.add_argument("--rules", default="syn")
    ap.add_argument("--timeline", default="", help="write a per-second replay file (one date) for the player")
    a = ap.parse_args()
    out = []
    for d in a.dates:
        try:
            tl = [] if a.timeline else None
            res = run_day(a.und.upper(), d, a.lots, a.buffer, a.entry, a.exit, rules=a.rules, timeline=tl)
            if tl is not None:
                extra = tl.pop()
                with open(a.timeline, "w", encoding="utf-8") as f:
                    json.dump({"summary": {k: v for k, v in res.items() if k != "path"}, "frames": tl, **extra}, f,
                              separators=(",", ":"), default=str)
        except Exception as e:                                  # a day without recordings etc.
            res = {"und": a.und, "date": d, "error": f"{type(e).__name__}: {e}"}
        out.append(res)
        print(json.dumps({k: v for k, v in res.items() if k != "path"}), flush=True)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
