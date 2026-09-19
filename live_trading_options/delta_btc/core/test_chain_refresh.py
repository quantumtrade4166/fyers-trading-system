"""core/test_chain_refresh.py — the live chain must adopt strikes listed after it was built.

2026-09-19: the chain was built with strikes up to 80000, BTC rose to 81,100, and
Delta listed 80200-83000 overnight. The chain never learned of them, ATM clamped
to 80000 and ist_day found no CE to sell all day.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.chain import LiveChain
from core.selector import select_entry_leg, CE, PE

PASS = FAIL = 0
def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")

EXP = "190926"
built = [{"symbol": f"{c}-BTC-{k}-{EXP}", "id": 1 + i * 2 + (c == "P"), "strike_price": str(k),
          "contract_value": "0.001", "tick_size": "0.1"}
         for i, k in enumerate(range(76000, 80001, 200)) for c in ("C", "P")]
ch = LiveChain(EXP).load(built)
check("built grid tops out at 80000", ch.strikes[-1], 80000.0)

def tick(strike, c, mark, spot=81135.0):
    return {"symbol": f"{c}-BTC-{strike}-{EXP}", "product_id": 900000 + strike + (c == "P"),
            "strike_price": str(strike), "mark_price": str(mark), "spot_price": str(spot),
            "quotes": {"best_bid": str(mark - 1), "best_ask": str(mark + 1)}}

# spot 81,135 with the new strikes now trading
tickers = [tick(k, "C", max(1.0, 1100 - (k - 80000) * 0.5)) for k in range(76000, 80001, 200)]
tickers += [tick(k, "C", m) for k, m in ((80200, 962), (80400, 770), (80600, 585), (80800, 413),
                                         (81000, 267), (81200, 159), (81400, 90), (81600, 53),
                                         (81800, 34), (82000, 25))]
tickers += [tick(k, "P", 5) for k in range(76000, 80001, 200)]
ch.refresh(tickers)
check("new strikes adopted", ch.new_strikes, 10)
check("grid now reaches 82000", ch.strikes[-1], 82000.0)
check("ATM follows the spot, not the old edge", ch.atm, 81200.0)
pick = select_entry_leg(ch.chain(), ch.atm, CE, target=50)
check("a CE near target is found again", pick and pick["strike"], 81600.0)
ch.refresh(tickers)
check("re-seeing them does not double count", ch.new_strikes, 10)
check("an adopted contract is tradeable", ch.contract(81600, CE)["symbol"], f"C-BTC-81600-{EXP}")

print(f"\n  {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
