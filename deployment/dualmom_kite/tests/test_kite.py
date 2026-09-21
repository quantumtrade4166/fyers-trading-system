"""
DualMom on the SHARED Kite account - offline tests. No broker, no network.

The fake account mixes DualMom orders with the strangle's option orders and a
manual holding, because the whole point of the Kite design is that only
DualMom's own tagged trades are ever counted.

    python -m deployment.dualmom_kite.tests.test_kite
"""

import json
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd

from deployment.dualmom_kite import config as C
from deployment.dualmom_kite import kite_equity as K
from deployment.dualmom_kite import ledger as L
from deployment.dualmom_kite import book as B
from deployment.dualmom_kite import engine as E
from deployment.dualmom_live import rebalance as R
from deployment.dualmom_live import config as SC

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="dmk_test_"))
L.STATE = TMP
L.ROOT = TMP / "ledger"
E.STATE = TMP
K.time.sleep = lambda s: None                     # no real waiting in tests


# ── a fake Kite account ──────────────────────────────────────────────────────

class FakeKite:
    def __init__(self):
        self.instr = [
            {"tradingsymbol": "RELIANCE", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.1, "instrument_token": 1},
            {"tradingsymbol": "HFCL-BE", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.01, "instrument_token": 2},
            {"tradingsymbol": "POWERINDIA", "segment": "NSE", "instrument_type": "EQ", "tick_size": 5.0, "instrument_token": 3},
            {"tradingsymbol": "IDEA-BZ", "segment": "NSE", "instrument_type": "EQ", "tick_size": 0.01, "instrument_token": 4},
            {"tradingsymbol": "RELIANCE26SEP", "segment": "NFO-FUT", "instrument_type": "FUT", "tick_size": 0.1, "instrument_token": 5},
        ]
        self.px = {"RELIANCE": 1300.0, "HFCL-BE": 220.0, "POWERINDIA": 30000.0}
        self.prev = {"RELIANCE": 1290.0, "HFCL-BE": 222.0, "POWERINDIA": 29500.0}
        self._orders, self._trades, self.n = [], [], 0
        self.fail_mode = None                    # None | "lost" | "margin" | "reject_fill"
        self.hold = [{"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 50, "t1_quantity": 0}]
        # strangle's option orders in the same account - must never be counted
        self._orders.append({"order_id": "S1", "tag": "vwstrangle", "tradingsymbol": "NIFTY26SEP25000CE",
                             "exchange": "NFO", "status": "COMPLETE", "transaction_type": "SELL",
                             "quantity": 75, "filled_quantity": 75, "average_price": 120.0, "product": "NRML"})
        self._trades.append({"trade_id": "ST1", "order_id": "S1", "tradingsymbol": "NIFTY26SEP25000CE",
                             "exchange": "NFO", "transaction_type": "SELL", "quantity": 75,
                             "average_price": 120.0, "product": "NRML", "exchange_timestamp": "2026-09-21 09:21:00"})
        self.margin = {"net": 620000.0, "available": {"cash": 620000.0, "live_balance": 620000.0, "collateral": 0}}

    def instruments(self, exch): return self.instr
    def quote(self, keys):
        out = {}
        for k in keys:
            ts = k.split(":", 1)[1]
            if ts in self.px:
                out[k] = {"last_price": self.px[ts], "ohlc": {"close": self.prev[ts]}}
        return out
    def margins(self, seg): return self.margin
    def holdings(self): return self.hold
    def positions(self):
        day = {}
        for o in self._orders:
            if o.get("product") != "CNC" or o["filled_quantity"] == 0:
                continue
            d = day.setdefault(o["tradingsymbol"], {"tradingsymbol": o["tradingsymbol"], "exchange": "NSE",
                                                    "product": "CNC", "buy_quantity": 0, "sell_quantity": 0})
            d["buy_quantity" if o["transaction_type"] == "BUY" else "sell_quantity"] += o["filled_quantity"]
        return {"day": list(day.values()), "net": []}
    def orders(self): return [dict(o) for o in self._orders]
    def trades(self): return [dict(t) for t in self._trades]
    def profile(self): return {"user_id": "TEST"}

    def place_order(self, **kw):
        if self.fail_mode == "margin":
            raise Exception("Insufficient funds. Required margin is 5000")
        self.n += 1
        oid = f"K{self.n}"
        rej = self.fail_mode == "reject_fill"
        px = self.px[kw["tradingsymbol"]]
        o = {"order_id": oid, "tag": kw["tag"], "tradingsymbol": kw["tradingsymbol"], "exchange": "NSE",
             "status": "REJECTED" if rej else "COMPLETE", "status_message": "RMS: blocked" if rej else None,
             "transaction_type": kw["transaction_type"], "quantity": kw["quantity"],
             "filled_quantity": 0 if rej else kw["quantity"], "average_price": 0 if rej else px,
             "price": kw["price"], "product": kw["product"], "order_type": kw["order_type"],
             "order_timestamp": datetime(2026, 9, 21, 11, 0, 0), "exchange_order_id": "X" + oid}
        self._orders.append(o)
        if not rej:
            self._trades.append({"trade_id": "T" + oid, "order_id": oid, "tradingsymbol": kw["tradingsymbol"],
                                 "exchange": "NSE", "transaction_type": kw["transaction_type"],
                                 "quantity": kw["quantity"], "average_price": px, "product": "CNC",
                                 "exchange_timestamp": datetime(2026, 9, 21, 11, 0, 1),
                                 "exchange_order_id": "X" + oid})
        if self.fail_mode == "lost":
            raise Exception("ReadTimeout")
        return oid


print("\n=== 1. token is read, never generated ===")
tf = TMP / "zerodha_token.json"
K.TOKEN_FILE = tf
tf.write_text(json.dumps({"access_token": "abc", "date": "2020-01-01"}))
check("yesterday's token is refused", K.read_token() == "")
tf.write_text(json.dumps({"access_token": "abc", "date": date.today().isoformat()}))
check("today's token is used", K.read_token() == "abc")
src = Path(K.__file__).read_text(encoding="utf-8")
check("kite_equity never calls generate_session / login",
      "generate_session" not in src and "auto_login" not in src)

print("\n=== 2. instruments: exact EQ / BE only ===")
kt = FakeKite()
r = K.resolve(kt, "RELIANCE")
check("EQ resolves to itself", r["trading_symbol"] == "RELIANCE" and r["series"] == "EQ")
r = K.resolve(kt, "HFCL")
check("BE-only name resolves to HFCL-BE with its tick", r["trading_symbol"] == "HFCL-BE" and r["tick_size"] == 0.01)
check("5.00 tick carried", K.resolve(kt, "POWERINDIA")["tick_size"] == 5.0)
for bad in ("IDEA", "RELI"):
    try:
        K.resolve(kt, bad)
        check(f"{bad} refused", False)
    except LookupError:
        check(f"{bad} refused (other series / no substring match)", True)
check("limit rounds to the 5.00 tick", K.marketable_limit(30000, "BUY", 0.005, 5.0) == 30150.0)
check("sell limit below mark", K.marketable_limit(1300, "SELL", 0.005, 0.1) == 1293.5)

print("\n=== 3. tags ===")
tags = [K.unique_tag() for _ in range(300)]
check("300 tags all unique", len(set(tags)) == 300)
check("<= 20 chars, alphanumeric", all(len(t) <= 20 and t.isalnum() for t in tags))
check("ours recognised", K.is_ours(tags[0]))
check("strangle tag is not ours", not K.is_ours("vwstrangle") and not K.is_ours("dnstrangle"))
check("bare prefix is not ours", not K.is_ours("dmk"))

print("\n=== 4. placing: lost response found by tag, never re-sent ===")
kt = FakeKite()
kt.fail_mode = "lost"
res = K.place_limit(kt, "RELIANCE", "BUY", 5, 1306.5, 0.1)
check("lost response -> PLACED via tag lookup", res["status"] == "PLACED" and res["order_id"] == "K1", res)
check("exactly one order reached the broker", kt.n == 1)
kt.fail_mode = "margin"
res = K.place_limit(kt, "RELIANCE", "BUY", 5, 1306.5, 0.1)
check("margin error flagged", res["status"] == "REJECTED" and res["margin"])
kt = FakeKite()
o1 = K.place_limit(kt, "RELIANCE", "BUY", 3, 1306.5, 0.1)
st = K.await_all(kt, [o1["order_id"]])
check("await_all reads COMPLETE + qty + avg", st[o1["order_id"]]["status"] == "COMPLETE"
      and st[o1["order_id"]]["filled_qty"] == 3 and st[o1["order_id"]]["avg_price"] == 1300.0)

print("\n=== 5. ledger records ONLY DualMom ===")
cap = L.capture(kt)
fills = L._read("fills.jsonl")
check("1 DualMom fill captured", len(fills) == 1 and fills[0]["symbol"] == "RELIANCE", cap)
check("strangle option fill NOT recorded", all(f["broker_order_id"] != "S1" for f in fills))
check("strangle order NOT recorded", all(o["broker_order_id"] != "S1" for o in L._read("orders.jsonl")))
check("capture is idempotent", L.capture(kt).get("fills") == 0 and len(L._read("fills.jsonl")) == 1)
check("fill carries exchange time + Zerodha rate card",
      fills[0]["exchange_time"] == "2026-09-21T11:00:01"
      and fills[0]["charges_est"]["rate_card"] == C.CHARGES_RATE_CARD["version"])
check("hash chains verify", all(v["ok"] for v in L.verify_all().values()))
raw = (L.ROOT / "fills.jsonl").read_text(encoding="utf-8")
(L.ROOT / "fills.jsonl").write_text(raw.replace('"qty":3', '"qty":4'), encoding="utf-8")
check("tampering is detected", not L.verify("fills.jsonl")["ok"])
(L.ROOT / "fills.jsonl").write_text(raw, encoding="utf-8")

print("\n=== 6. own cash & NAV ignore everything that is not DualMom ===")
oc = L.own_cash()
exp = C.CAPITAL_BASE - 3 * 1300.0 - fills[0]["charges_est"]["total"]
check("own cash = capital - buys - charges", abs(oc["cash"] - exp) < 0.01, oc)
sell = [{"symbol": "X", "side": "SELL", "notional": 1000.0, "exchange_time": "2026-10-01T10:00:00",
         "charges_est": {"total": 1.0}},
        {"symbol": "X", "side": "SELL", "notional": 500.0, "exchange_time": "2026-10-01T11:00:00",
         "charges_est": {"total": 0.5}}]
check("DP charge once per scrip per sell day",
      abs(L.own_cash(sell)["dp_est"] - C.CHARGES_RATE_CARD["dp_per_sell_scrip_day"]) < 1e-9)
bk = B.live(kt, use_cache=False)
check("book shows only RELIANCE (not INFY, not the option)",
      [r["symbol"] for r in bk["rows"]] == ["RELIANCE"], [r["symbol"] for r in bk["rows"]])
check("NAV = own shares + own cash", abs(bk["nav"] - (3 * 1300.0 + oc["cash"])) < 0.01, bk["nav"])
check("account margin is shown but not counted as cash", bk["account_margin"]["net"] == 620000.0
      and bk["cash"] < C.CAPITAL_BASE)
check("recon OK when broker holds our qty", bk["reconciliation"]["ok"], bk["reconciliation"])
kt.hold.append({"tradingsymbol": "RELIANCE", "exchange": "NSE", "quantity": 10, "t1_quantity": 0})
bk = B.live(kt, use_cache=False)
check("owner's extra shares of the same stock: still OK, shown as extra",
      bk["reconciliation"]["ok"] and bk["rows"][0]["broker_extra"] == 10)
kt.hold = [h for h in kt.hold if h["tradingsymbol"] != "RELIANCE"]
kt._orders = [o for o in kt._orders if o["order_id"] != "K1"]      # shares gone from the account
bk = B.live(kt, use_cache=False)
check("broker holding LESS than ours is a break", not bk["reconciliation"]["ok"])

print("\n=== 7. reserve parameter leaves Kotak unchanged ===")
sig = {"signal": "IN", "date": "2026-08-31",
       "holdings": [{"symbol": "RELIANCE", "weight": 0.5}, {"symbol": "HFCL", "weight": 0.5}]}
marks = {"RELIANCE": 1300.0, "HFCL": 220.0}
a = R.plan(sig, {}, marks, 1_000_000)
b = R.plan(sig, {}, marks, 1_000_000, reserve=SC.CASH_RESERVE_RS)
check("default reserve == Kotak's Rs 15,000", a["buy_value"] == b["buy_value"])
c = R.plan(sig, {}, marks, 600_000, reserve=C.CASH_RESERVE_RS)
check("Kite sizes 6L less its own 10k reserve", 585_000 < c["buy_value"] <= 590_000, c["buy_value"])

print("\n=== 8. engine: plan + execute on the fake account ===")
shutil.rmtree(L.ROOT, ignore_errors=True)
from deployment.dualmom_live import signal_engine as S
_dates = pd.to_datetime(["2026-08-28", "2026-08-31", "2026-09-01"])
S.load_prices = lambda symbols=None: pd.DataFrame(index=_dates)
S.compute = lambda as_of=None, prices=None: {
    "signal": "IN", "date": str(as_of), "nifty_close": 24000.0, "nifty_ma": 23000.0,
    "universe_valid": 480, "holdings": [
        {"symbol": "RELIANCE", "weight": 0.45, "price": 1290.0},
        {"symbol": "HFCL", "weight": 0.45, "price": 225.0},
        {"symbol": "POWERINDIA", "weight": 0.10, "price": 29000.0}]}
check("month signal date = last session before the 1st",
      str(E.month_signal_date(date(2026, 9, 21))) == "2026-08-31")
kt = FakeKite()
run = E.build_plan(kt, capital=600_000)
p = run["plan"]
check("plan built at LIVE Kite prices", all(o["mark"] == kt.px[o["trading_symbol"]] for o in p["buys"]))
check("BE name sent as HFCL-BE", any(o["trading_symbol"] == "HFCL-BE" for o in p["buys"]))
check("margin preview present", p["account_margin"]["net"] == 620000.0 and p["account_margin"]["basket_needs"] > 0)
check("plan safe", p["safe"], p["blocked"])
C.DRY_RUN = True
r0 = E.execute(json.loads(json.dumps(run, default=str)), kt)
check("DRY_RUN sends nothing", kt.n == 0 and "DRY RUN" in (r0["halted"] or ""))
C.DRY_RUN = False
run = E.execute(run, kt)
done, summ = E.complete(run)
check("all buys FILLED", done, summ)
check("every order has a unique DualMom tag", len({o["tag"] for o in run["orders"]}) == len(run["orders"])
      and all(K.is_ours(o["tag"]) for o in run["orders"]))
L.capture(kt)
check("own book = exactly what was bought",
      {s: p_["qty"] for s, p_ in L.own_book()["positions"].items()} ==
      {o["symbol"]: o["qty"] for o in run["orders"]})
try:
    E.build_plan(kt, capital=600_000)
    check("capital override refused once positions exist", False)
except ValueError:
    check("capital override refused once positions exist", True)

print("\n=== 9. deploy guards ===")
E.datetime = type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 9, 21, 11, 0, tzinfo=tz))})
why = E.deploy_guards(kt)
check("filled today -> refused", any("already filled today" in w for w in why), why)
E.gate_mark_done("2026-09", {"test": True})
check("month already done -> refused", any("already done" in w for w in E.deploy_guards(FakeKite())))
(TMP / "month_gate.json").unlink()
E.datetime = type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 9, 21, 8, 0, tzinfo=tz))})
check("before market -> refused", any("outside" in w for w in E.deploy_guards(FakeKite())))
E.datetime = type("D", (datetime,), {"now": classmethod(lambda cls, tz=None: datetime(2026, 9, 21, 11, 0, tzinfo=tz))})
check("clean account in market hours -> clear", E.deploy_guards(FakeKite()) == [], E.deploy_guards(FakeKite()))

print("\n=== 10. a SELL that does not fill stops the buys ===")
kt2 = FakeKite()
kt2.fail_mode = "reject_fill"
run2 = {"plan": {"safe": True, "blocked": [], "sells": [{"symbol": "RELIANCE", "side": "SELL", "qty": 2, "mark": 1300.0}],
                 "buys": [{"symbol": "HFCL", "side": "BUY", "qty": 10, "mark": 220.0}]}, "log": []}
run2 = E.execute(run2, kt2)
check("buys not sent after an unfilled sell", "buys NOT sent" in (run2["halted"] or "")
      and all(o["side"] == "SELL" for o in run2["orders"]), run2["halted"])
kt3 = FakeKite()
kt3.fail_mode = "margin"
run3 = {"plan": {"safe": True, "blocked": [], "sells": [],
                 "buys": [{"symbol": "HFCL", "side": "BUY", "qty": 10, "mark": 220.0},
                          {"symbol": "RELIANCE", "side": "BUY", "qty": 1, "mark": 1300.0}]}, "log": []}
run3 = E.execute(run3, kt3)
check("margin rejection halts, no retry", "margin rejection" in (run3["halted"] or "") and len(run3["orders"]) == 1)

print("\n=== 11. stop check uses only our book ===")
shutil.rmtree(L.ROOT, ignore_errors=True)
kt4 = FakeKite()
K.place_limit(kt4, "RELIANCE", "BUY", 4, 1306.5, 0.1)
L.capture(kt4)
kt4.px["RELIANCE"] = 1300.0 * 0.64                     # -36%
br = E.stop_breaches(kt4)
check("-36% breaches the -35% stop, sells OUR 4 only",
      len(br) == 1 and br[0]["symbol"] == "RELIANCE" and br[0]["qty"] == 4, br)
kt4.px["RELIANCE"] = 1300.0 * 0.70
check("-30% does not breach", E.stop_breaches(kt4) == [])

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n  {len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
