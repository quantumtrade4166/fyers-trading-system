"""EXIT DUALMOM'S KITE BOOK - and nothing else in that account.

    python _kite_flatten.py              DRY RUN: boundary + sell list + P&L
    python _kite_flatten.py --yes        send the sell orders

THE ACCOUNT IS SHARED. Positions come from DualMom's OWN LEDGER
(dualmom_kite/ledger.own_book, built from fills tagged `dmk`), never from the
broker's holdings. Anything else sitting in that Kite account - other strategies,
the owner's own shares - is listed as HANDS OFF and is never in an order.

That distinction is the whole reason the Kite module keeps its own book while
the Kotak one can treat its account as dedicated. Selling "the holdings" here
would sell someone else's stock.

Everything goes through engine.execute(), so it inherits the resolve brake (an
unresolvable SELL halts rather than exiting a partial book), the circuit
handling, and the ledger capture.

CDSL: until DDPI is active on this account, Kotak-style delivery sells get
rejected with "shares need to be authorised at CDSL". The script reports that
distinctly from a real failure, because it is a one-time account setup, not a bug.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"C:\trading\fyers_data_pipeline")
sys.path.insert(0, str(ROOT))
for line in (ROOT / "deployment" / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    s = line.strip()
    if s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

LIVE = "--yes" in sys.argv

from deployment.dualmom_kite import config as KC
from deployment.dualmom_kite import engine as KE
from deployment.dualmom_kite import kite_equity as KK
from deployment.dualmom_kite import ledger as KL
from deployment.dualmom_live import rebalance as R

print(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  mode={'LIVE' if LIVE else 'DRY RUN'} ===")
print(f"    ENABLED={KC.ENABLED}  DRY_RUN={KC.DRY_RUN}  tag={KC.ORDER_TAG}")
if not KC.ENABLED or KC.DRY_RUN:
    print("!! dualmom_kite config.ENABLED is False or DRY_RUN is True - execute() would")
    print("   refuse and send nothing. Stopping so that is not mistaken for an exit.")
    sys.exit(2)

if not KK.read_token():
    print("!! no Kite access token for today (ZerodhaAutoLogin runs 09:00). Stopping.")
    sys.exit(2)

kite = KK.client()

# ---- ledger integrity first: this is the list we will trade from ------------
print("\n=== ledger integrity (the source of truth for what we own) ===")
bad = []
for name, r in KL.verify_all().items():
    print(f"    {name:<16} ok={r['ok']} records={r['records']} {r['reason'] or ''}")
    if not r["ok"]:
        bad.append(name)
if bad:
    print(f"!! hash chain broken in {bad} - refusing to trade from a ledger that may")
    print("   have been altered. Investigate before exiting.")
    sys.exit(3)

# ---- capture first, so the book reflects anything filled earlier today -----
try:
    cap = KL.capture(kite)
    print(f"    capture before planning: {cap}")
except Exception as e:
    print(f"    capture failed (continuing on the stored book): {type(e).__name__}")

current, nav, cash, marks = KE.own_state(kite)
own = {s: q for s, q in current.items() if q > 0}
if not own:
    print("\n   DualMom holds nothing in Kite - nothing to do")
    sys.exit(0)

# ---- the boundary ----------------------------------------------------------
broker = {k: v for k, v in KK.broker_equity_qty(kite).items() if v.get("qty", 0) > 0}
hands_off = sorted(set(broker) - set(own))
ghost = sorted(set(own) - set(broker))

print(f"\n=== BOUNDARY ===")
print(f"    DualMom's own book : {len(own)} positions  <- these and only these")
print(f"    others in account  : {len(hands_off)}")
if hands_off:
    print(f"\n    --- HANDS OFF (in the account, NOT DualMom's, never ordered) ---")
    for s in hands_off:
        print(f"      {s:<14} qty={broker[s]['qty']:>6}  avg={broker[s].get('broker_avg') or 0:>10,.2f}")
if ghost:
    print(f"\n    !! ledger says we hold these but the broker does not: {ghost}")
    print(f"       a SELL for them would be rejected; investigate before --yes")

mismatch = [(s, own[s], broker[s]["qty"]) for s in sorted(set(own) & set(broker))
            if own[s] != broker[s]["qty"]]
if mismatch:
    print(f"\n    !! quantity mismatches (ledger vs broker):")
    for s, lq, bq in mismatch:
        print(f"      {s:<14} ledger={lq} broker={bq}")

# ---- build the exit plan from the LEDGER ------------------------------------
sig = {"signal": "OUT", "date": datetime.now().strftime("%Y-%m-%d"), "holdings": []}
plan = R.plan(sig, own, marks, nav, reserve=KC.CASH_RESERVE_RS)

print(f"\n=== EXIT PLAN ===")
print(f"    sells {len(plan['sells'])}   buys {len(plan['buys'])}   safe={plan['safe']}")
for b in plan.get("blocked") or []:
    print(f"    BLOCKED: {b}")

ob = KL.own_book()
cost = sum(p["cost"] for p in ob["positions"].values())
proceeds = sum(o["value"] for o in plan["sells"])
print(f"\n    {'symbol':<14}{'qty':>7}{'mark':>11}{'value':>12}{'cost':>12}{'P&L':>11}")
tot_pnl = 0.0
for o in sorted(plan["sells"], key=lambda x: -x["value"]):
    p = ob["positions"].get(o["symbol"]) or {}
    c = p.get("cost", 0.0)
    pnl = o["value"] - c
    tot_pnl += pnl
    print(f"    {o['symbol']:<14}{o['qty']:>7}{o['mark']:>11,.2f}{o['value']:>12,.0f}"
          f"{c:>12,.0f}{pnl:>11,.0f}")
print(f"    {'TOTAL':<14}{'':>7}{'':>11}{proceeds:>12,.0f}{cost:>12,.0f}{tot_pnl:>11,.0f}")

charges = proceeds * 0.00126
print(f"\n    capital in        : Rs {KC.contributed():,.0f}")
print(f"    own cash          : Rs {cash:,.2f}")
print(f"    gross proceeds  ~ : Rs {proceeds:,.0f}")
print(f"    est sell costs  ~ : Rs {charges:,.0f}")
print(f"    MTM             ~ : Rs {tot_pnl:,.0f}")
print(f"    MTM net of costs ~: Rs {tot_pnl - charges:,.0f}")

if not LIVE:
    print(f"\n    DRY RUN - nothing sent. Re-run with --yes to exit DualMom's {len(own)} positions.")
    sys.exit(0)

# ---- send -------------------------------------------------------------------
print(f"\n=== SENDING {len(plan['sells'])} SELL ORDERS (tag {KC.ORDER_TAG}) ===")
run = {"started": datetime.now().isoformat(timespec="seconds"), "log": [],
       "trigger": "manual_flatten", "signal": sig, "plan": plan}
run = KE.execute(run, kite)
KE.save(run, f"flatten_{datetime.now():%Y%m%d_%H%M%S}.json")
for line in run.get("log", []):
    print(f"    {line}")
try:
    KL.capture(kite)
except Exception:
    pass

orders = run.get("orders") or []
filled = [o for o in orders if str(o.get("status")) == "FILLED"]
cdsl = [o for o in orders if "cdsl" in str(o.get("reason", "")).lower()
        or "authoris" in str(o.get("reason", "")).lower()]

print(f"\n=== RESULT ===")
print(f"    sent   : {len(orders)}")
print(f"    filled : {len(filled)}")
print(f"    halted : {run.get('halted') or 'no'}")
if cdsl:
    print(f"\n    {len(cdsl)} order(s) rejected for CDSL authorisation.")
    print("    That is the DDPI setup, not a code failure - once DDPI is active on")
    print("    this Zerodha account, re-run and they will go through.")
unfilled = [o for o in orders if str(o.get("status")) != "FILLED"]
if unfilled:
    print(f"\n    not filled ({len(unfilled)}):")
    for o in unfilled[:40]:
        print(f"      {o.get('symbol'):<14} {o.get('status'):<12} {str(o.get('reason') or '')[:70]}")
    print("\n    Re-run to retry the remainder.")
else:
    print(f"\n    DualMom is FULLY OUT of Kite. Other holdings untouched.")
