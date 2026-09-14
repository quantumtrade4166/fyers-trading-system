"""core/test_fills.py — the book walk that prices every paper fill.

Run:  .venv/Scripts/python.exe live_trading_options/delta_btc/core/test_fills.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.fills import walk_book

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


bids = [{"price": "40.0", "size": 600}, {"price": "39.5", "size": 300},
        {"price": "39.0", "size": 5000}]
w = walk_book(bids, 1000)
check("sell 1000 walks three bid levels", w["levels_used"], 3)
check("average is size-weighted: (600*40 + 300*39.5 + 100*39) / 1000", w["price"], 39.75)
check("top is the best bid", w["top"], 40.0)
check("worst is the last level reached", w["worst"], 39.0)
check("book was deep enough", w["short"], 0)

check("an order inside level one gets level one exactly",
      walk_book(bids, 500)["price"], 40.0)
check("an order inside level one uses one level", walk_book(bids, 500)["levels_used"], 1)

asks = [(41.0, 487), (42.0, 3412)]
a = walk_book(asks, 1000)
check("buy walks UP through asks: (487*41 + 513*42) / 1000", a["price"], 41.513)
check("buy uses two levels", a["levels_used"], 2)

thin = walk_book([(10.0, 200), (9.0, 300)], 1000)
check("thin book: shortfall reported", thin["short"], 500)
check("thin book: shortfall priced at the worst visible level",
      thin["price"], round((200 * 10 + 300 * 9 + 500 * 9) / 1000, 4))

check("empty book fills nothing", walk_book([], 1000)["price"], None)
check("empty book is fully short", walk_book([], 1000)["short"], 1000)
check("zero-size levels are skipped",
      walk_book([(50.0, 0), (49.0, 1000)], 1000)["price"], 49.0)

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
