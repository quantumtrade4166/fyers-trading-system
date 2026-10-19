"""
tools/vps/builder_readiness.py — is the Strategy Builder ready to place a live order? READ-ONLY.
Places NO order. Checks, per index and per logged-in broker:
  1. the builder's live chain answers and is ticking
  2. the broker resolves the exact ATM CE/PE contract (its own instrument list)
  3. the broker's order book gives a marketable price for 1 lot
  4. Zerodha: real basket margin for a 1-lot ATM short straddle

    .venv\\Scripts\\python.exe tools\\vps\\builder_readiness.py [--skip kotak_bhaiya]
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from live_trading_options.strategy_builder import brokers as B      # noqa: E402
from live_trading_options.strategy_builder import orders as O       # noqa: E402

skip = set(sys.argv[sys.argv.index("--skip") + 1].split(",")) if "--skip" in sys.argv else set()


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:47674{path}", timeout=15) as r:
        return json.loads(r.read())


chains = {}
print("== 1. live chain")
for idx in ("NIFTY", "SENSEX"):
    c = get(f"/api/sb/chain?index={idx}")
    if not c.get("ok"):
        print(f"  {idx}: NOT READY {c}")
        continue
    chains[idx] = c
    atm = next(r for r in c["rows"] if r["strike"] == c["atm"])
    print(f"  {idx}: expiry {c['expiry']}  spot {c['under']['spot']}  synth {c['synthetic_fut']:.1f}  ATM {c['atm']:.0f}"
          f"  CE {atm['CE']['ltp']} / PE {atm['PE']['ltp']}  last tick {c['tick_age']}s ago  rows {len(c['rows'])}")

ad = {"kite": O.KiteAdapter(B.kx), "kotak": O.KotakAdapter(B.ke)}
print("\n== 2-4. brokers (no orders)")
for a in B.BrokerHub().accounts.values():
    if a.id in skip or a.parked:
        print(f"  {a.name}: skipped")
        continue
    if not a.login():
        print(f"  {a.name}: LOGIN FAILED — {a.error}")
        continue
    print(f"  {a.name}: logged in, funds {a.funds}")
    for idx, c in chains.items():
        atm = next(r for r in c["rows"] if r["strike"] == c["atm"])
        for typ in ("CE", "PE"):
            leg = {"index": idx, "expiry": c["expiry"], "strike": c["atm"], "type": typ}
            try:
                k = B.resolve(a, leg)
                fb = atm[typ]["ltp"]
                px_b = ad[a.kind].price(a.client, k, "B", int(k["lot"]), fb * 1.1, 2)
                px_s = ad[a.kind].price(a.client, k, "S", int(k["lot"]), fb * 0.9, 2)
                print(f"     {idx} {c['atm']:.0f}{typ}: {k['symbol']} lot {k['lot']}  "
                      f"fyers ltp {fb}  -> marketable BUY {px_b} / SELL {px_s}")
            except Exception as e:
                print(f"     {idx} {c['atm']:.0f}{typ}: FAILED {type(e).__name__}: {e}")
        if a.kind == "kite":
            try:
                orders = []
                for typ in ("CE", "PE"):
                    k = B.resolve(a, {"index": idx, "expiry": c["expiry"], "strike": c["atm"], "type": typ})
                    orders.append({"exchange": k["exchange"], "tradingsymbol": k["symbol"], "transaction_type": "SELL",
                                   "variety": "regular", "product": "NRML", "order_type": "MARKET",
                                   "quantity": int(k["lot"]), "price": 0})
                m = a.client.basket_order_margins(orders, consider_positions=True, mode="compact")
                print(f"     {idx} 1-lot ATM short straddle margin (Zerodha): "
                      f"{(m.get('final') or m.get('initial') or {}).get('total')}")
            except Exception as e:
                print(f"     {idx} margin: FAILED {type(e).__name__}: {e}")
print("\nreadiness check done — nothing was ordered")
