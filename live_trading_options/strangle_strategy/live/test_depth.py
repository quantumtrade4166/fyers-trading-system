"""test_depth.py — order-book pricing for the shared executor.

Pricing a marketable limit off a multiple of the last mark is a guess: too tight
and it never fills, too wide and it can sweep a thin book and fill somewhere
terrible. `sweep_price` walks the real levels instead. These tests pin the cases
that matter on the exit path, where not filling means staying naked.

    .venv\\Scripts\\python.exe live_trading_options/strangle_strategy/live/test_depth.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kite_executor import sweep_price, marketable_price, quote_book, BUY, SELL

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"    ok   {name}")
    else:
        FAIL += 1
        print(f"    FAIL {name}: got {got!r}, want {want!r}")


def lv(*pairs):
    return [{"price": p, "quantity": q} for p, q in pairs]


print("\n  ── sweep_price: buying against the offers ──")
# a deep book: 65 lots clear at the very first level
book = lv((31.0, 500), (31.5, 400), (32.0, 900))
check("deep book fills at the touch + 2 ticks",
      sweep_price(book, 65, BUY, cushion_ticks=2), 31.10)

# a THIN book: the first level cannot cover us, so we must reach deeper
thin = lv((31.0, 25), (33.0, 25), (36.0, 400))
check("thin book walks to the level that clears the size",
      sweep_price(thin, 65, BUY, cushion_ticks=2), 36.10)

# size larger than the whole visible book -> deepest visible price, still real
check("oversized order anchors to the deepest visible level",
      sweep_price(thin, 100000, BUY, cushion_ticks=2), 36.10)

check("empty book returns None so the caller can fall back",
      sweep_price([], 65, BUY), None)

print("\n  ── cushion grows in TICKS, not percentages ──")
check("cushion 0", sweep_price(book, 65, BUY, cushion_ticks=0), 31.0)
check("cushion 10", sweep_price(book, 65, BUY, cushion_ticks=10), 31.50)
check("cushion is tick-rounded", sweep_price(book, 65, BUY, cushion_ticks=3), 31.15)

print("\n  ── selling works the other way ──")
bids = lv((30.0, 500), (29.5, 400))
check("sell prices DOWN through the bids",
      sweep_price(bids, 65, SELL, cushion_ticks=2), 29.90)

print("\n  ── marketable_price falls back when the book cannot be read ──")


class _NoQuote:
    def quote(self, keys):
        raise RuntimeError("quote unavailable")


class _Book:
    def __init__(self, sell, buy=()):
        self._sell, self._buy = list(sell), list(buy)

    def quote(self, keys):
        return {keys[0]: {"last_price": 31.0,
                          "depth": {"buy": self._buy, "sell": self._sell}}}


check("broker refuses the quote -> fallback used",
      marketable_price(_NoQuote(), "NFO", "X", BUY, 65, fallback=99.0), 99.0)
check("book present -> depth wins over the fallback",
      marketable_price(_Book(lv((31.0, 500))), "NFO", "X", BUY, 65, fallback=99.0), 31.10)
check("empty depth -> fallback used",
      marketable_price(_Book([]), "NFO", "X", BUY, 65, fallback=99.0), 99.0)
check("quote_book on a dead symbol is None", quote_book(_NoQuote(), "NFO", "X"), None)

print("\n  ── the point of all this ──")
# the old behaviour: mark 31 * 1.9 = 58.9 cap on a thin book could sweep to 58.9.
# depth pricing reaches only as far as the size actually requires.
old_style = round(31.0 * 1.9, 2)
new_style = sweep_price(thin, 65, BUY, cushion_ticks=2)
check("depth price is well inside a blind 90% multiple", new_style < old_style, True)
print(f"       blind multiple would cap at {old_style}; the book says {new_style}")

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
