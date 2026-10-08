"""
tools/vwap_live_control.py — arm / disarm / kill the live BTC VWAP strangle, and check it.
=========================================================================================

Run ON THE VPS (it reads deployment/.env for the Delta keys):

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\tools\\vwap_live_control.py --status
    ... --check-keys     read-only: wallet + positions (proves the key, signature and IP)
    ... --test-order     sends ONE 1-contract IOC SELL priced 10x above the market on the
                         cheapest far-OTM call — it cannot fill, Delta cancels it at once.
                         Proves order permission end to end. Needs the word YES.
    ... --arm            mode = live  (takes effect when the book is flat)
    ... --disarm         mode = paper (takes effect when the book is flat)
    ... --kill           close every open leg now, no more entries today

The engine reads data/vwap_state/live_control_ist_live.json every few seconds.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse
import datetime as dt

VERSION = "ist_delta"          # "Vwap New Delta Ex" — the only live-capable version
STATE = ROOT / "data" / "vwap_state"
CTL = STATE / f"live_control_{VERSION}.json"


def read_ctl():
    try:
        return json.loads(CTL.read_text(encoding="utf-8"))
    except Exception:
        return {"mode": "paper", "kill": False}


def write_ctl(**kw):
    d = read_ctl()
    d.update(kw, updated=dt.datetime.now().isoformat(timespec="seconds"))
    STATE.mkdir(parents=True, exist_ok=True)
    CTL.write_text(json.dumps(d, indent=2), encoding="utf-8")
    print(f"  control -> {d}")


def status():
    print(f"  control file : {read_ctl()}")
    try:
        s = json.loads((STATE / f"{VERSION}.json").read_text(encoding="utf-8"))
        print(f"  engine       : mode {s.get('mode')}  status {s.get('status')}  updated {s.get('updated')}")
        print(f"  strikes      : {s.get('pair', {}) and (s['pair'].get('ce'), s['pair'].get('pe'))}  "
              f"entries {s.get('entries')}  price MTM ${s.get('price_mtm')}  stop {s.get('stop_reason')}")
        for t, l in (s.get("legs") or {}).items():
            if l:
                print(f"  open {t}      : {l['qty']} @ {l['sell']}  kept={l.get('kept')}  "
                      f"pending={l.get('exit_pending')}  mode={l.get('mode')}")
    except Exception as e:
        print(f"  engine state : unavailable ({e})")


def check_keys():
    from live.delta_client import DeltaClient
    c = DeltaClient()
    print(f"  USD available: {c.usd_available()}")
    for w in c.wallet() or []:
        if float(w.get("balance") or 0):
            print(f"  wallet {w.get('asset_symbol')}: balance {w.get('balance')} "
                  f"available {w.get('available_balance')}")
    pos = c.raw_positions()
    print(f"  open positions: {len(pos)}")
    for p in pos:
        print(f"    {(p.get('product') or {}).get('symbol') or p.get('product_symbol') or p.get('product_id')}"
              f"  size {p.get('size')}  entry {p.get('entry_price')}")
    print("  KEYS OK (signed read worked from this IP)")
    try:
        from core.api import btc_option_tickers
        tk = [t for t in btc_option_tickers() if t["symbol"].startswith("C-")]
        t = min(tk, key=lambda x: abs(float(x.get("strike_price") or 0) - float(x.get("spot_price") or 0)))
        print(f"  leverage on {t['symbol']}: {c.get_leverage(int(t['product_id']))}x (read-only)")
    except Exception as e:
        print(f"  leverage read failed: {e}")


def test_order():
    if input("  Send a 1-contract IOC SELL that cannot fill (10x the market)? type YES: ").strip() != "YES":
        print("  cancelled")
        return
    from core.api import btc_option_tickers
    from live.delta_client import DeltaClient, order_result
    tk = [t for t in btc_option_tickers() if t["symbol"].startswith("C-")
          and (t.get("quotes") or {}).get("best_bid")]
    t = min(tk, key=lambda x: float(x.get("mark_price") or 1e9))
    mark = float(t["mark_price"])
    px = max(round(mark * 10, 1), 10.0)
    c = DeltaClient()
    o = c.place_order(int(t["product_id"]), "sell", 1, limit_price=px, tif="ioc",
                      client_order_id=f"bvwtest{dt.datetime.now():%H%M%S}")
    r = order_result(o, 1)
    print(f"  {t['symbol']} mark {mark} -> sell 1 @ {px}: state {r['state']} filled {r['filled']}")
    print("  ORDER PATH OK" if r["filled"] == 0 else "  !! IT FILLED — check the account")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for f in ("status", "check-keys", "test-order", "arm", "disarm", "kill"):
        g.add_argument("--" + f, action="store_true")
    a = ap.parse_args()
    if a.status:
        status()
    elif a.check_keys:
        check_keys()
    elif a.test_order:
        test_order()
    elif a.arm:
        write_ctl(mode="live", kill=False)
        print("  ARMED: the engine goes live when ist_live is flat")
    elif a.disarm:
        write_ctl(mode="paper")
        print("  DISARMED: paper from the next flat moment")
    elif a.kill:
        write_ctl(kill=True)
        print("  KILL sent: open legs are being bought back, no entries today")


if __name__ == "__main__":
    main()
