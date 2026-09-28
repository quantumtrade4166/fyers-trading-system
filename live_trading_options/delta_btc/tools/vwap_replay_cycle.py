"""
tools/vwap_replay_cycle.py — rebuild a live cycle's trades under the CURRENT rules.
===================================================================================

Why this exists
---------------
On 2026-09-16 the full_expiry cycle was ended at 00:10 by two things that are no
longer in the strategy:

  1. a 1000-contract market sell swept a 119-contract top level and filled at 117
     against a 150.2 trigger (a 22% round trip) — the new bad-fill guard REFUSES
     that fill and keeps the signal armed instead;
  2. that instant spread showed as -$51.80 MTM and tripped a $50 cycle stop that
     has since been removed (mtm_stop_usd = 0).

So the book's state was an artifact of a bug, not of the strategy. This tool
replays the cycle from the ENGINE'S OWN CANDLES plus the minute-by-minute chain
ARCHIVE (data/chain_archive/{date}/chunk_*.parquet), running the same Trigger the
live engine runs, and writes the corrected trades and position back into the
resume file so the engine carries on from there.

What is real and what is reconstructed
--------------------------------------
  - candles / VWAP: untouched, exactly what the engine built live
  - trades that closed BEFORE `--from`: untouched, real fills
  - trades after it: reconstructed. Entry and exit prices come from the archived
    best bid / best ask of that minute — the depth walk cannot be redone after the
    fact, so these are top-of-book prices, and each such trade is tagged
    `"replayed": true`. A REPLAY line is written to the audit log.

Run with the engine STOPPED:
    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\tools\\vwap_replay_cycle.py \\
        --version full_expiry --from "2026-09-16 00:00" [--apply]

Without --apply it only prints what it would do.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import glob
import json
import argparse
import datetime as dt

from vwap.strategy import Trigger, Session

PARAMS = json.loads((ROOT / "config" / "vwap_parameters.json").read_text(encoding="utf-8"))
STATE = ROOT / "data" / "vwap_state"
RESULTS = ROOT / "data" / "vwap_results"
LOGS = ROOT / "logs"


def load_archive(symbols, start: dt.datetime, end: dt.datetime) -> dict:
    """{minute -> {"mark": (ce, pe), "bid": (ce, pe), "ask": (ce, pe)}} from the
    1-minute chain snapshots, for the two legs only."""
    import pandas as pd
    out = {}
    days = {start.date(), end.date()}
    frames = []
    for d in sorted(days):
        for f in sorted(glob.glob(str(ROOT / "data" / "chain_archive" / d.isoformat() / "chunk_*.parquet"))):
            try:
                df = pd.read_parquet(f)
            except Exception:
                continue
            df = df[df["symbol"].isin(symbols)]
            if len(df):
                frames.append(df)
    if not frames:
        return out
    df = pd.concat(frames)
    df["minute"] = df["captured"].astype(str).str[:16]
    for minute, g in df.groupby("minute"):
        row = {}
        for _, r in g.iterrows():
            row[r["symbol"]] = r
        if len(row) < 2:
            continue
        ce, pe = row.get(symbols[0]), row.get(symbols[1])
        if ce is None or pe is None:
            continue

        def pair(col):
            a, b = ce.get(col), pe.get(col)
            if a is None or b is None:
                return None
            try:
                a, b = float(a), float(b)
            except (TypeError, ValueError):
                return None
            return None if (a != a or b != b) else (a, b)

        m, bid, ask = pair("mark"), pair("best_bid"), pair("best_ask")
        if m is None:
            continue
        out[minute] = {"mark": m, "bid": bid, "ask": ask}
    return out


def fee_for(price, contracts, spot=None, cv=0.001):
    f = PARAMS.get("fees") or {}
    qty = contracts * cv
    prem = float(f.get("premium_cap_rate", 0.035)) * price * qty
    if spot:
        return round(min(float(f.get("taker_rate_notional", 0.0001)) * spot * qty, prem), 6)
    return round(prem, 6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="full_expiry")
    ap.add_argument("--from", dest="frm", required=True,
                    help='rebuild everything at or after this IST time, e.g. "2026-09-16 00:00"')
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    cfg = PARAMS["versions"][a.version]
    sess = Session(a.version, cfg)
    contracts = sess.contracts
    max_slip = float(PARAMS.get("max_entry_slippage_pct", 5)) / 100.0
    offset = float(PARAMS.get("trigger_offset", 1.0))
    minutes = int(PARAMS.get("candle_minutes", 5))

    f = STATE / f"{a.version}_resume.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    frm = dt.datetime.fromisoformat(a.frm.replace(" ", "T"))
    candles = [dict(c, start=dt.datetime.fromisoformat(c["start"]))
               for c in d["builder"]["closed"]]
    bounds = (dt.datetime.fromisoformat(d["key"]), None)
    cutoff = sess.cutoff_dt(sess.bounds(candles[-1]["start"]))

    kept = [t for t in (d.get("trades") or [])
            if t.get("exit_time") and dt.datetime.fromisoformat(t["exit_time"]) < frm]
    dropped = [t for t in (d.get("trades") or []) if t not in kept]
    print(f"cycle {d['key']}  candles {len(candles)}  kept trades {len(kept)}  "
          f"rebuilt from {frm} (dropping {len(dropped)})")
    for t in dropped:
        print(f"  dropping: #{t['n']} {t['entry_time']} {t['entry_combined']} -> "
              f"{t.get('exit_time')} {t.get('exit_combined')} net {t.get('net_usd')} "
              f"({t.get('exit_reason')})")

    pair = d["pair"]
    syms = [pair["ce_symbol"], pair["pe_symbol"]]
    arch = load_archive(syms, frm, candles[-1]["start"] + dt.timedelta(minutes=minutes))
    print(f"archive minutes available: {len(arch)}")
    if not arch:
        print("no archived chain rows — cannot replay honestly. Aborting.")
        return

    trades, state = list(kept), {"pos": None, "n": len(kept)}

    def price_at(minute, side):
        row = arch.get(minute)
        if not row:
            return None
        legs = row.get("bid" if side == "SELL" else "ask") or row["mark"]
        return round(legs[0] + legs[1], 4), legs

    def on_entry(trigger, n, reason):
        minute = state["minute"]
        got = price_at(minute, "SELL")
        if got is None:
            return "retry"
        comb, legs = got
        if comb < trigger * (1 - max_slip):
            return "retry"          # the guard: nothing is sold, stays armed
        fees = fee_for(legs[0], contracts) + fee_for(legs[1], contracts)
        state["pos"] = {
            "n": n, "signal": reason, "trigger": trigger,
            "entry_time": f"{minute}:00", "ce_sell": legs[0], "pe_sell": legs[1],
            "entry_combined": comb, "entry_slippage": round(comb - trigger, 4),
            "entry_fees": round(fees, 6), "entry_src": "archive/top-of-book",
            "ce_exit": None, "pe_exit": None, "exit_pending": None, "replayed": True,
        }
        print(f"  ENTRY  #{n} {minute} trigger {trigger:.2f} sold {comb:.2f} "
              f"(CE {legs[0]} + PE {legs[1]})")
        return True

    def on_exit(n, reason):
        minute = state["minute"]
        got = price_at(minute, "BUY")
        if got is None:
            return False
        comb, legs = got
        p = state["pos"]
        fees = p["entry_fees"] + fee_for(legs[0], contracts) + fee_for(legs[1], contracts)
        pts = round(p["entry_combined"] - comb, 4)
        gross = round(pts * contracts * 0.001, 4)
        trades.append(dict(p, exit_time=f"{minute}:00", exit_reason=reason,
                           ce_exit=legs[0], pe_exit=legs[1], exit_combined=comb,
                           points=pts, gross_usd=gross, fees=round(fees, 4),
                           net_usd=round(gross - fees, 4), version=a.version,
                           cycle=d["key"], expiry=d["expiry"],
                           ce_strike=pair["ce"], pe_strike=pair["pe"],
                           contracts=contracts, replayed=True))
        print(f"  EXIT   #{n} {minute} bought {comb:.2f} -> net ${trades[-1]['net_usd']:+.2f}")
        state["pos"] = None
        return True

    tr = Trigger(on_entry, on_exit, max_entries=sess.max_entries, cutoff=cutoff,
                 offset=offset, minutes=minutes)
    tr.entries = len(kept)

    for c in candles:
        if c["start"] + dt.timedelta(minutes=minutes) <= frm:
            continue
        # walk the candle minute by minute: an entry fills the moment the combined
        # mark reaches the resting trigger, exactly as the live tick path does
        for k in range(minutes):
            t = c["start"] + dt.timedelta(minutes=k)
            state["minute"] = f"{t:%Y-%m-%d %H:%M}"
            row = arch.get(state["minute"])
            if row:
                tr.on_tick(round(row["mark"][0] + row["mark"][1], 4), t)
        state["minute"] = f"{c['start'] + dt.timedelta(minutes=minutes):%Y-%m-%d %H:%M}"
        tr.in_pos = state["pos"] is not None
        tr.on_candle_close(c)

    realized = round(sum(t["net_usd"] for t in trades), 2)
    print(f"\nrebuilt: {len(trades)} trades, realized ${realized:+.2f}, "
          f"position {'OPEN ' + str(state['pos']['entry_combined']) if state['pos'] else 'flat'}, "
          f"entries used {tr.entries}/{sess.max_entries}")

    if not a.apply:
        print("\n(dry run — pass --apply to write it back)")
        return

    d["trades"] = trades
    d["pos"] = state["pos"]
    d["trigger"] = dict(tr.to_dict(), in_pos=state["pos"] is not None)
    d["stop_reason"] = None
    f.write_text(json.dumps(d, default=str), encoding="utf-8")

    with open(LOGS / f"{dt.datetime.now():%Y-%m-%d}_btc_vwap_audit.log", "a",
              encoding="utf-8") as lg:
        lg.write(json.dumps({
            "ts": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "version": a.version,
            "cycle": d["key"], "event": "REPLAY",
            "reason": "cycle rebuilt under current rules (bad-fill guard + no MTM stop); "
                      "trades after the cutoff are reconstructed from the chain archive "
                      "at top-of-book prices",
            "from": a.frm, "dropped": len(dropped), "trades": len(trades),
            "realized": realized, "open": bool(state["pos"]),
        }) + "\n")
    print("written. Restart the engine — it resumes from this state.")


if __name__ == "__main__":
    main()
