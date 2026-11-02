"""
cepe_imbalance/dry_run.py — live feed check without the web server. PAPER, nothing saved, no orders.

    .venv\\Scripts\\python.exe -m live_trading_options.cepe_imbalance.dry_run --secs 40
"""
import json
import logging
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

from live_trading_options.cepe_imbalance import instruments
from live_trading_options.cepe_imbalance.engine import Engine
from live_trading_options.cepe_imbalance.feed import MultiFeed
from live_trading_options.cepe_imbalance.market import Market
from live_trading_options.mcx_terminal import kotak_session

SECS = int(sys.argv[sys.argv.index("--secs") + 1]) if "--secs" in sys.argv else 40


def main():
    logging.basicConfig(level=logging.WARNING)
    sessions = kotak_session.SessionHolder()
    u = instruments.load(sessions.get().client)
    m = Market(u)
    eng = Engine(u, m, persist=False)
    feed = MultiFeed(sessions, eng.on_ticks)
    eng.feed = feed
    eng.refresh_subscriptions()
    feed.start()
    t0 = time.time()
    while time.time() - t0 < 12:
        time.sleep(1)
        eng.refresh_subscriptions()                    # windows join once ATM is known
    atm = m.atm("CRUDEOIL")
    print("paper strangle CRUDEOIL ATM", atm)
    if atm:
        print(" ", eng.manual_order("CRUDEOIL", atm + 100, "CE", "SELL", 1))
        print(" ", eng.manual_order("CRUDEOIL", atm - 100, "PE", "SELL", 1))
    time.sleep(max(0, SECS - 12))
    s = eng.summary()
    print("feed:", s["feed"])
    for r in s["underlyings"]:
        print(f"{r['name']:9} exp {r['expiry']} ref {r['ref']} (age {r['ref_age']}s) ATM {r['atm']} "
              f"mtm {r['mtm']['total']} imb {r['imbalance']['pct']} session {r['session']}")
    for n in ("CRUDEOIL", "GOLDM", "NIFTY"):
        rows = m.chain_rows(n, m.expiry(n), 2)
        print(n, json.dumps([{"k": r["strike"], "atm": r["atm"], "CE": (r["CE"]["bid"], r["CE"]["ask"], r["CE"]["age"]),
                              "PE": (r["PE"]["bid"], r["PE"]["ask"], r["PE"]["age"])} for r in rows]))
    print("positions CRUDEOIL:", eng.runners["CRUDEOIL"].book.open_positions())
    feed.stop()


if __name__ == "__main__":
    main()
