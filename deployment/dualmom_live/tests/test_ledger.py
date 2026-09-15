"""
Tests for the client ledger + live book, using the EXACT row shapes Kotak returned
live on 2026-09-15. No broker, no network; the ledger writes to a temp directory.

    python -m deployment.dualmom_live.tests.test_ledger
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

from deployment.dualmom_live import config as C
from deployment.dualmom_live import ledger as L
from deployment.dualmom_live import book as B
from deployment.dualmom_live import kotak_equity as K

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


TMP = Path(tempfile.mkdtemp(prefix="dm_ledger_test_"))
L.ROOT = TMP / "ledger"
_runs = TMP / "runs"
_runs.mkdir()
L._runs_dir = lambda: _runs


def fill_row(oid, flid, sym, side, qty, px, tm, tag, leg=1):
    """Shape of a live Kotak trade_report row (2026-09-15)."""
    return {"actId": "15P56", "algCat": "NA", "algId": "99999", "avgPrc": f"{px:.2f}",
            "boeSec": 1789451016, "exOrdId": "1000000030306333", "exSeg": "nse_cm",
            "exTm": tm, "fldQty": qty, "flDt": "15-Sep-2026", "flId": flid, "flLeg": leg,
            "flTm": tm[-8:], "nOrdNo": oid, "prcTp": "L", "prod": "CNC", "rptTp": "fill",
            "series": "EQ", "sym": sym, "trdSym": f"{sym}-EQ",
            "trnsTp": "B" if side == "BUY" else "S", "usrId": "BKCPC1838D",
            "hsUpTm": "2026/09/15 11:13:36", "GuiOrdId": tag, "it": "EQ"}


class Broker:
    def __init__(self):
        self.trades, self.orders, self.pos, self.hold, self.cash = [], [], [], [], 16_473.21

    def trade_report(self):
        return {"stat": "ok", "stCode": 200, "data": self.trades}

    def order_report(self):
        return {"stat": "Ok", "stCode": 200, "data": self.orders}

    def positions(self):
        return {"stat": "ok", "stCode": 200, "data": self.pos}

    def holdings(self):
        if not self.hold:
            return {"error": [{"code": 424, "message": "No holdings found for this client 15P56"}]}
        return {"data": self.hold}

    def limits(self, **kw):
        return {"Net": str(self.cash), "stat": "Ok"}


print("\n=== 1. hash chain: append-only, tamper-evident ===")
L._append("events.jsonl", [{"key": "a", "kind": "x"}, {"key": "b", "kind": "y"}])
L._append("events.jsonl", [{"key": "c", "kind": "z"}])
check("clean chain verifies", L.verify("events.jsonl")["ok"], L.verify("events.jsonl"))
p = L._path("events.jsonl")
lines = p.read_text(encoding="utf-8").splitlines()
p.write_text("\n".join([lines[0], lines[1].replace('"y"', '"EDITED"'), lines[2]]) + "\n", encoding="utf-8")
v = L.verify("events.jsonl")
check("an edited record is detected, at the right line",
      not v["ok"] and v["broken_at_seq"] == 2, v)
p.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
v = L.verify("events.jsonl")
check("a deleted record is detected", not v["ok"], v)
p.unlink()

print("\n=== 2. capture is idempotent and keeps the raw payload ===")
b = Broker()
b.trades = [fill_row("260915000319957", "3417390", "ATHERENERG", "BUY", 48, 1595.60,
                     "15-Sep-2026 11:13:36", "dm15111335001"),
            fill_row("260915000319966", "3417401", "HFCL", "BUY", 200, 223.50,
                     "15-Sep-2026 11:13:37", "dm15111336002"),
            fill_row("260915000319966", "3417402", "HFCL", "BUY", 62, 223.55,
                     "15-Sep-2026 11:13:37", "dm15111336002", leg=2)]
(_runs / "run_20260915_111417.json").write_text(json.dumps({
    "started": "2026-09-15T11:12:00", "signal": {"date": "2026-08-31", "signal": "IN",
    "holdings": [{"symbol": "ATHERENERG", "weight": 0.0783}]},
    "orders": [{"symbol": "ATHERENERG", "side": "BUY", "qty": 48, "mark": 1599.20,
                "limit": 1607.2, "order_id": "260915000319957", "tag": "dm15111335001",
                "status": "FILLED", "target_qty": 48}]}), encoding="utf-8")
n1 = L.capture_fills(b)
n2 = L.capture_fills(b)
check("3 fills captured (HFCL filled in 2 legs = 2 records)", n1 == 3, n1)
check("second capture writes nothing", n2 == 0, n2)
f = L._read("fills.jsonl")[0]
check("raw broker payload stored verbatim", f["raw"]["flId"] == "3417390")
check("exchange time parsed to ISO", f["exchange_time"] == "2026-09-15T11:13:36", f["exchange_time"])
check("decision context joined from the run file",
      f["decision_mark"] == 1599.20 and f["signal_date"] == "2026-08-31", f)
check("slippage vs decision is negative when filled BETTER than the mark",
      f["slippage_bps_vs_decision"] < 0, f["slippage_bps_vs_decision"])
check("fills chain verifies", L.verify("fills.jsonl")["ok"])

print("\n=== 3. attribution ===")
check("dm + digits -> dualmom", L.attribute("dm15111335001") == "dualmom")
check("the go-live test tag", L.attribute("dualmom") == "dualmom_golive_test")
check("anything else -> unattributed (e.g. a manual app trade)",
      L.attribute("") == "unattributed" and L.attribute("vwsk101500123") == "unattributed")

print("\n=== 4. statutory charges estimate (delivery) ===")
c = L.estimate_charges("BUY", 100_000)
check("STT 0.1% on buy = Rs 100", abs(c["stt"] - 100) < 1e-6, c)
check("stamp 0.015% on buy = Rs 15", abs(c["stamp"] - 15) < 1e-6, c)
check("no stamp duty on a sell", L.estimate_charges("SELL", 100_000)["stamp"] == 0)
check("flagged as estimate with unverified brokerage",
      c["estimate"] is True and c["brokerage_verified"] is False)

print("\n=== 5. FIFO own book ===")
fl = [{"symbol": "X", "side": "BUY", "qty": 10, "price": 100.0, "exchange_time": "2026-09-15T10:00:00", "exchange_fill_id": "1"},
      {"symbol": "X", "side": "BUY", "qty": 10, "price": 110.0, "exchange_time": "2026-09-16T10:00:00", "exchange_fill_id": "2"},
      {"symbol": "X", "side": "SELL", "qty": 15, "price": 120.0, "exchange_time": "2026-09-17T10:00:00", "exchange_fill_id": "3"}]
ob = L.own_book(fl)
check("realized = 10x(120-100) + 5x(120-110) = 250", abs(ob["realized"] - 250) < 1e-9, ob["realized"])
check("remaining 5 shares at the SECOND lot's price (FIFO, not average)",
      ob["positions"]["X"]["qty"] == 5 and abs(ob["positions"]["X"]["avg_price"] - 110) < 1e-9,
      ob["positions"]["X"])
ob2 = L.own_book()
check("HFCL rebuilt from its 2 legs = 262 shares",
      ob2["positions"]["HFCL"]["qty"] == 262, ob2["positions"].get("HFCL"))

print("\n=== 6. broker book: unsettled positions + settled holdings merged ===")
b.pos = [{"actId": "15P56", "exSeg": "nse_cm", "prod": "CNC", "sym": "ATHERENERG",
          "trdSym": "ATHERENERG-EQ", "series": "EQ", "flBuyQty": "48", "flSellQty": "0",
          "buyAmt": "76588.80", "sellAmt": "0.00", "cfBuyQty": "0", "tok": "757645"},
         {"actId": "15P56", "exSeg": "nse_cm", "prod": "CNC", "sym": "HFCL",
          "trdSym": "HFCL-BE", "series": "BE", "flBuyQty": "262", "flSellQty": "0",
          "buyAmt": "58560.10", "sellAmt": "0.00", "cfBuyQty": "0", "tok": "21954"}]
bp = B.broker_positions(b)
check("holdings empty on purchase day does not hide today's buys", set(bp) == {"ATHERENERG", "HFCL"}, bp)
check("avg from buyAmt / flBuyQty", abs(bp["ATHERENERG"]["avg_price"] - 1595.60) < 1e-6,
      bp["ATHERENERG"]["avg_price"])
b.hold = [{"displaySymbol": "ATHERENERG-EQ", "quantity": 48, "averagePrice": "1595.60"}]
b.pos = b.pos[1:]
bp = B.broker_positions(b)
check("after settlement: holdings + remaining positions, no double count",
      bp["ATHERENERG"]["qty"] == 48 and bp["HFCL"]["qty"] == 262, bp)
check("a -BE symbol is normalised to its base name", "HFCL" in bp and "HFCL-BE" not in bp)

print("\n=== 7. live book + reconciliation ===")
B.quotes = lambda client, syms, tokens=None: {"ATHERENERG": {"ltp": 1650.0, "change": 20.0, "change_pct": 1.23},
                                  "HFCL": {"ltp": 220.0, "change": -1.0, "change_pct": -0.45}}
bk = B.live(b, use_cache=False)
check("reconciles: broker qty == ledger qty for every name", bk["reconciliation"]["ok"], bk["reconciliation"])
at = next(r for r in bk["rows"] if r["symbol"] == "ATHERENERG")
check("MTM = 48 x (1650 - 1595.60) = 2611.20", abs(at["mtm"] - 2611.20) < 0.01, at["mtm"])
check("NAV = market value + cash", abs(bk["nav"] - (bk["market_value"] + bk["cash"])) < 0.01)
check("stop price = avg x 0.65", abs(at["stop_price"] - round(1595.60 * 0.65, 2)) < 0.01, at["stop_price"])
buys_notional = sum(x["notional"] for x in L._read("fills.jsonl"))
b.cash = C.CAPITAL_BASE - buys_notional                  # charges NOT yet debited
bk = B.live(b, use_cache=False)
check("undebited charges shown as pending", bk["charges_pending_est"] > 0, bk["charges_pending_est"])
check("charges-adjusted NAV = NAV - pending",
      abs(bk["nav_after_pending_charges"] - (bk["nav"] - bk["charges_pending_est"])) < 0.01)
b.cash = C.CAPITAL_BASE - buys_notional - 1_166.71      # Kotak has now debited them
bk = B.live(b, use_cache=False)
check("once debited, pending drops to zero (never subtracted twice)",
      bk["charges_pending_est"] == 0, bk["charges_pending_est"])
b.cash = 16_473.21
b.hold = [{"displaySymbol": "ATHERENERG-EQ", "quantity": 50, "averagePrice": "1595.60"}]
bk = B.live(b, use_cache=False)
check("a manual extra 2 shares in the app shows as a reconciliation BREAK",
      not bk["reconciliation"]["ok"] and bk["reconciliation"]["breaks"][0]["symbol"] == "ATHERENERG",
      bk["reconciliation"])

print("\n=== 8. equity + drawdown series ===")
L.record_daily({"date": "2026-09-16", "time": "15:30:00", "nav": 1_020_000}, {})
L.record_daily({"date": "2026-09-17", "time": "15:30:00", "nav": 969_000}, {})
check("a second EOD row for the same date is refused",
      L.record_daily({"date": "2026-09-17", "time": "15:30:00", "nav": 1}, {}) is False)
ser = B.equity_series()
navs = [p["nav"] for p in ser["equity"]]
check("starts at the capital base", navs[0] == C.CAPITAL_BASE, navs)
check("max drawdown = 969000/1020000 - 1 = -5%", abs(ser["max_drawdown_pct"] - (-5.0)) < 1e-6,
      ser["max_drawdown_pct"])
check("events chain still verifies after EOD writes", L.verify("events.jsonl")["ok"])

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for fname in FAIL:
    print(f"    FAILED: {fname}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
