"""
tools/preflight.py — what will the engine do today?
===================================================

Prints the rules the delta-neutral engine will actually apply, resolved against
the live Kite instrument dump: which indices trade, their DTE, the strikes and
lot sizes available, the entry target, and the full stop schedule with the times
each step becomes due.

Run it before arming. It answers "is today a trade day, and on what terms" from
the same code paths the engine uses, so it cannot drift from reality the way a
written note would.

Read-only. Never places an order, never starts a socket.

    .venv\\Scripts\\python.exe live_trading_options/delta_neutral/tools/preflight.py
"""

import sys
import json
import argparse
import datetime as dt
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.broker import kite_executor as kx, read_control
from core.chain import LiveChain, is_trade_day

P = json.loads((ROOT / "config" / "parameters.json").read_text())


def main():
    now = dt.datetime.now()
    print(f"\n  today: {now:%Y-%m-%d %A}   preflight run at {now:%H:%M:%S}\n")
    try:
        k = kx.get_kite()
        print(f"  kite      : ok ({k.profile().get('user_id')})")
    except Exception as e:
        print(f"  kite      : UNAVAILABLE — {e}")
        print("              the engine cannot trade without this; fix the token first.")
        return

    gap = P.get("sl_arm_gap", 10)
    for index in P["live_orders"].get("indices", []):
        print(f"\n  ══ {index} ══")
        try:
            tradeable, dte, expiry = is_trade_day(k, index, day)
        except Exception as e:
            print(f"    expiry lookup FAILED: {e}")
            continue
        print(f"    expiry    : {expiry}   DTE {dte}")
        if not tradeable:
            print(f"    verdict   : NO TRADE today (strategy trades DTE 0 and 1 only)")
            print(f"    the chain still streams, so the tab shows prices — no orders.")
            continue

        rule = (P["entry"].get(index, {}) or {}).get(str(dte)) or {}
        if not rule:
            print(f"    verdict   : NO RULE configured for DTE {dte} — will not trade")
            continue

        try:
            chain = LiveChain(index, expiry, P["strike_interval"][index], k,
                              span=P.get("chain_span", 15)).load()
            lot = chain.lot_size
            print(f"    contracts : {len(chain.strikes)} strikes "
                  f"({chain.strikes[0]}..{chain.strikes[-1]}, step {P['strike_interval'][index]})"
                  f"   lot {lot}")
        except Exception as e:
            print(f"    chain load FAILED: {e}")
            continue

        band = rule.get("prefer_min"), rule.get("prefer_max")
        band_txt = f"  (prefer {band[0]}-{band[1]})" if band[0] else ""
        print(f"    entry     : {P.get('entry_time','09:30')} at premium ~{rule['target']}{band_txt}, never ATM")

        sched = rule.get("sl_schedule") or []
        if sched:
            steps = "  ".join(f"-> {s['sl']} at {s['from']}" for s in sched)
            print(f"    stop      : {rule['sl']} from the open   {steps}")
            cur = rule["sl"]
            for s in sched:
                arm = min(cur - gap, s["sl"])
                print(f"                step to {s['sl']} arms only when BOTH legs are "
                      f"below {arm}")
                cur = s["sl"]
        else:
            print(f"    stop      : {rule['sl']} flat all day (no schedule on DTE {dte})")

        buf = P.get("sl_limit_buffer", 2)
        buf = buf.get(index, 2) if isinstance(buf, dict) else buf
        print(f"    sl order  : trigger T, limit T+{buf}   "
              f"(watchdog force-covers {P.get('sl_watchdog_gap',10)} past T)")
        print(f"    adjust    : {P['adjust_trigger_ratio']}x rule, 1-min windows "
              f"{P['adjust_windows']['first']}..{P['adjust_windows']['last']} "
              f"every {P['adjust_windows']['every_minutes']}min")
        print(f"    exit      : {P.get('square_off','15:14')}   "
              f"max {P.get('max_fresh_entries',3)} fresh entries")

        c = read_control(index)
        armed = c.get("mode") == "live"
        q = c.get("qty") or lot
        print(f"    ARMED     : {'*** YES — REAL ORDERS ***' if armed else 'no (paper)'}"
              f"   qty {q} ({q // lot} lot{'s' if q // lot != 1 else ''})"
              f"   max loss {c.get('mtm_stop') or P.get('max_loss')}")
        if armed:
            worst = round((rule["sl"] - rule["target"]) * q * 2)
            print(f"                both legs stopping at the OPENING stop ~ Rs{worst:,}")

    print()


if __name__ == "__main__":
    main()
