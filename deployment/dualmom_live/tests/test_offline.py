"""
Offline tests for dualmom_live. No Kotak login, no token, no network.

Runs safely while the market is open and the Vwap Strangle holds the account's
single Kotak session — nothing here touches a broker.

Live Order Safety rule 7: "paper always fills, so paper hides all of this."
So the stub broker below REFUSES: margin shortfalls, bad sides, partial fills,
orders that never fill. Those paths are the whole point.

    python -m deployment.dualmom_live.tests.test_offline
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

from deployment.dualmom_live import config as C
from deployment.dualmom_live import kotak_equity as K
from deployment.dualmom_live import rebalance as R

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


# ── stub broker ──────────────────────────────────────────────────────────────

class StubKotak:
    """Mimics Kotak's habits: returns error DICTS, never raises; spells fields
    inconsistently; validates transaction_type client-side like the real SDK."""

    def __init__(self, mode="ok", cash=1_000_000.0):
        self.mode, self.cash, self.orders, self.n = mode, cash, {}, 0

    def place_order(self, **kw):
        if kw.get("transaction_type") not in ("B", "S", "Buy", "Sell"):
            raise ValueError("Invalid transaction type. Allowed values are B or Buy, S or Sell.")
        if self.mode == "margin":
            return {"error": [{"message": "Insufficient funds. Margin required: 250000."}]}
        if self.mode == "reject":
            return {"error": [{"message": "RMS rule violated"}]}
        self.n += 1
        oid = f"2609070000{self.n:05d}"
        qty = int(kw["quantity"])
        filled = {"ok": qty, "partial": qty // 2, "nofill": 0}[
            self.mode if self.mode in ("ok", "partial", "nofill") else "ok"]
        self.orders[oid] = {"nOrdNo": oid, "ordSt": "complete" if filled == qty else "open",
                            "fldQty": str(filled), "avgPrc": kw["price"],
                            "tag": kw.get("tag", "")}
        return {"stat": "Ok", "nOrdNo": oid, "stCode": 200}

    def order_report(self):
        return {"data": list(self.orders.values())} if self.orders else {"stCode": 5203, "errMsg": "No Data"}

    def holdings(self):
        return {"data": [
            {"displaySymbol": "RELIANCE-EQ", "quantity": 10, "averagePrice": "1200.00"},
            {"displaySymbol": "TCS", "quantity": 5, "averagePrice": "3800.00"},
            {"displaySymbol": "ZERO", "quantity": 0, "averagePrice": "10"},
        ]}

    def limits(self, **kw):
        """Kotak's real behaviour (verified on a funded account 2026-09-10):
        the FILTERED call returns a well-formed ALL-ZERO response, and only the
        unfiltered call carries the money. Returning an error would have been
        honest; returning zeros silently reported a funded account as empty."""
        if kw.get("segment") == "CASH" or kw.get("exchange") or kw.get("product"):
            return {"Net": "0", "CollateralValue": "0", "stat": "Ok"}
        return {"Net": str(self.cash), "CollateralValue": str(self.cash), "stat": "Ok"}

    def quotes(self, instrument_tokens=None, **kw):
        """Kotak returns a LIST of rows (verified live 2026-09-10), and this SDK
        build REJECTS isIndex/quote_type. The old last_price() assumed a dict and
        raised AttributeError on every call, which silently disabled the stop."""
        if kw:
            raise TypeError(f"NeoAPI.quotes() got an unexpected keyword argument "
                            f"'{sorted(kw)[0]}'")
        px = {"2885": "1263.0000", "21954": "235.0900", "11821": "2687.0000"}
        out = []
        for t in (instrument_tokens or []):
            tok = str(t.get("instrument_token"))
            if tok in px:
                out.append({"exchange_token": tok, "ltp": px[tok],
                            "ohlc": {"close": px[tok]}})
        return out

    def search_scrip(self, exchange_segment=None, symbol=None):
        """Real nse_cm shapes captured from the live API on 2026-09-08.

        Includes the LTIM trap: Kotak's search is a SUBSTRING search and returns
        scrips that merely contain the letters, without the one you asked for.
        """
        db = {
            "RELIANCE":   [dict(pSymbolName="RELIANCE", pTrdSymbol="RELIANCE-EQ",
                                pGroup="EQ", pSymbol=2885, dTickSize=10)],
            "HFCL":       [dict(pSymbolName="HFCL", pTrdSymbol="HFCL-BE",
                                pGroup="BE", pSymbol=21954, dTickSize=1)],
            "POWERINDIA": [dict(pSymbolName="POWERINDIA", pTrdSymbol="POWERINDIA-EQ",
                                pGroup="EQ", pSymbol=18457, dTickSize=500)],
            "TCS":        [dict(pSymbolName="TCS", pTrdSymbol="TCS-EQ",
                                pGroup="EQ", pSymbol=11536, dTickSize=5)],
            # the real trap: neither row IS 'LTIM'
            "LTIM":       [dict(pSymbolName="EMULTIMQ", pTrdSymbol="EMULTIMQ-EQ",
                                pGroup="EQ", pSymbol=25996, dTickSize=1),
                           dict(pSymbolName="ALLTIME", pTrdSymbol="ALLTIME-EQ",
                                pGroup="EQ", pSymbol=758448, dTickSize=1)],
            "SUSPENDED":  [dict(pSymbolName="SUSPENDED", pTrdSymbol="SUSPENDED-SM",
                                pGroup="SM", pSymbol=999, dTickSize=5)],
        }
        return {"data": db.get((symbol or "").upper(), [])}


print("\n=== 1. transaction_type mapping (the 2026-09-03 live failure) ===")
check("SELL -> S", K._txn_code("SELL") == "S")
check("BUY -> B", K._txn_code("BUY") == "B")
check("lowercase sell -> S", K._txn_code("sell") == "S")
check("already-coded S stays S", K._txn_code("S") == "S")
try:
    K._txn_code("SHORT"); check("bad side raises", False)
except ValueError:
    check("bad side raises", True)

print("\n=== 2. rejections are DATA, not exceptions ===")
r = K.place_limit(StubKotak("margin"), "RELIANCE-EQ", "BUY", 10, 1200)
check("margin refusal returns REJECTED", r["status"] == "REJECTED", r)
check("margin refusal flagged as margin", r["margin"] is True, r)
check("no order id on refusal", r["order_id"] is None)
r = K.place_limit(StubKotak("reject"), "RELIANCE-EQ", "BUY", 10, 1200)
check("RMS refusal returns REJECTED", r["status"] == "REJECTED")
check("RMS refusal NOT margin", r["margin"] is False)
r = K.place_limit(StubKotak(), "RELIANCE-EQ", "SELL", 10, 1200)
check("good order returns PLACED + id", r["status"] == "PLACED" and r["order_id"])
check("qty<=0 is skipped not sent", K.place_limit(StubKotak(), "X", "BUY", 0, 5)["status"] == "SKIPPED")

print("\n=== 3. never write a fill you have not seen ===")
s = StubKotak("nofill")
r = K.place_limit(s, "RELIANCE-EQ", "BUY", 10, 1200)
st = K.order_status(s, r["order_id"])
check("unfilled order reports filled_qty=0", st["filled_qty"] == 0, st)
check("unfilled order is not 'complete'", "complete" not in st["status"])
s = StubKotak("partial")
r = K.place_limit(s, "RELIANCE-EQ", "BUY", 10, 1200)
st = K.order_status(s, r["order_id"])
check("partial fill reports the REAL qty", st["filled_qty"] == 5, st)
check("unknown order id -> not_found", K.order_status(StubKotak(), "999")["status"] == "not_found")

print("\n=== 4. margin classifier ===")
check("insufficient funds", K.is_margin_error("Insufficient funds. Margin required: 250000."))
check("margin shortfall", K.is_margin_error("margin shortfall on order"))
check("RMS is not margin", not K.is_margin_error("RMS rule violated"))

print("\n=== 5. holdings + NAV ===")
h = K.holdings(StubKotak())
check("parses 2 real holdings", set(h) == {"RELIANCE", "TCS"}, sorted(h))
check("strips the -EQ suffix", "RELIANCE" in h)
check("drops zero-qty rows", "ZERO" not in h)
av = K.account_value(StubKotak(), {"RELIANCE": 1300.0, "TCS": 4000.0})
check("NAV = mark-to-market + cash", av["nav"] == round(10*1300 + 5*4000 + 1_000_000, 2), av["nav"])

print("\n=== 6. marketable limit prices THROUGH the touch ===")
check("buy limit above mark", K.marketable_limit(100.0, "BUY") > 100)
check("sell limit below mark", K.marketable_limit(100.0, "SELL") < 100)
check("rounded to tick", abs(K.marketable_limit(100.03, "BUY") * 100 % 5) < 1e-6)

print("\n=== 7. allocation rules ===")
w = {"A": 0.5, "B": 0.3, "C": 0.2}
q = R.allocate(w, {"A": 100.0, "B": 50.0, "C": 25.0}, 100_000)
check("spends nearly everything", sum(q[s]*p for s, p in [("A",100),("B",50),("C",25)]) > 99_000, q)
q2 = R.allocate({"A": 0.5, "EXP": 0.5}, {"A": 100.0, "EXP": 500_000.0}, 100_000)
check("unaffordable name skipped", "EXP" not in q2, q2)
check("its weight redistributed, not idle", q2.get("A", 0) * 100 > 99_000, q2)
q3 = R.allocate({"A": 0.98, "P": 0.02}, {"A": 10.0, "P": 3_000.0}, 100_000)
check("2x ceiling blocks an oversized share", q3.get("P", 0) * 3000 <= 100_000*0.02*2 + 1, q3)

print("\n=== 8. delta rebalance ===")
sig = {"date": "2026-08-31", "signal": "IN",
       "holdings": [{"symbol": "A", "weight": 0.5}, {"symbol": "B", "weight": 0.5}]}
p = R.plan(sig, {"A": 100, "C": 50}, {"A": 100.0, "B": 100.0, "C": 100.0}, 100_000)
syms = {r["symbol"]: r for r in p["sells"] + p["buys"]}
check("C fully exited", syms.get("C", {}).get("side") == "SELL" and syms["C"]["qty"] == 50, syms.get("C"))
check("B newly bought", syms.get("B", {}).get("side") == "BUY", syms.get("B"))
a = syms.get("A", {})
check("A traded as a DELTA, not sell-all-then-rebuy",
      a.get("qty") == abs(a.get("target_qty", 0) - a.get("current_qty", 0))
      and a.get("current_qty") == 100, a)
check("sells listed before buys", p["sells"] and p["buys"])
check("buys flagged needs_proceeds (T+1)", all(b.get("needs_proceeds") for b in p["buys"]))

out = R.plan({"date": "x", "signal": "OUT", "holdings": []},
             {"A": 10, "B": 20}, {"A": 100.0, "B": 50.0}, 100_000)
check("OUT signal sells everything", len(out["sells"]) == 2 and not out["buys"])

print("\n=== 9. safety brakes ===")
big = R.plan(sig, {f"S{i}": 100 for i in range(200)},
             {**{f"S{i}": 100.0 for i in range(200)}, "A": 100.0, "B": 100.0}, 100_000)
check("absurd order count blocked", not big["safe"], big["blocked"])
check("blocked plan explains why", len(big["blocked"]) > 0)
check("normal plan is safe", p["safe"], p["blocked"])

print("\n=== 10. stop-loss detection (-35%) ===")
br = R.stop_breaches({"A": 10, "B": 10, "C": 10},
                     {"A": 100.0, "B": 100.0, "C": 100.0},
                     {"A": 64.0, "B": 66.0, "C": 100.0})
hit = {b["symbol"] for b in br}
check("-36% breaches", "A" in hit, hit)
check("-34% does NOT breach", "B" not in hit, hit)
check("flat does not breach", "C" not in hit, hit)
check("stop order is a full-size SELL", br and br[0]["qty"] == 10 and br[0]["side"] == "SELL")

print("\n=== 11. config is inert ===")
# Armed deliberately on 2026-09-15 after go-live step 6 passed live. The two flags
# must still move together: ENABLED without DRY_RUN=False (or the reverse) is a
# half-armed state where the UI and the jobs disagree about whether orders go out.
check("ENABLED and DRY_RUN are consistent (armed together)",
      C.ENABLED is (not C.DRY_RUN), f"ENABLED={C.ENABLED} DRY_RUN={C.DRY_RUN}")
check("uses the SEPARATE DualMom account", C.SESSION_POLICY == "own" and C.CREDENTIAL_PREFIX == "KOTAK_DM_")
check("tag does not collide with the strangles",
      C.ORDER_TAG not in ("vwstrangle", "dnstrangle", "vwstk_kotak"))
check("TOP_N is 40 (paper engine still says 50)", C.TOP_N == 40)
check("universe guard is RELATIVE not absolute",
      C.MIN_UNIVERSE_FRAC >= 0.85 and C.UNIVERSE_REF_DAYS >= 20)
check("10%% concentration cap adopted", C.MAX_WEIGHT == 0.10)
check("sizing slippage matches the backtest", C.SIZING_SLIPPAGE == 0.001)
check("limit buffer is separate from sizing", C.MARKETABLE_BUFFER != C.SIZING_SLIPPAGE)


print("\n=== 12. resolve() against real nse_cm shapes (captured live 2026-09-08) ===")
_b = StubKotak()
_r = K.resolve(_b, "RELIANCE")
check("RELIANCE -> RELIANCE-EQ", _r["trading_symbol"] == "RELIANCE-EQ", _r["trading_symbol"])
check("tick paise -> rupees (10 -> 0.10)", abs(_r["tick_size"] - 0.10) < 1e-9, _r["tick_size"])
check("RELIANCE not trade-for-trade", _r["is_trade_for_trade"] is False)
_h = K.resolve(_b, "HFCL")
check("HFCL -> HFCL-BE (BE series accepted)", _h["trading_symbol"] == "HFCL-BE", _h["trading_symbol"])
check("HFCL flagged trade-for-trade", _h["is_trade_for_trade"] is True)
_p = K.resolve(_b, "POWERINDIA")
check("POWERINDIA 500 paise -> tick 5.00", abs(_p["tick_size"] - 5.0) < 1e-9, _p["tick_size"])
try:
    K.resolve(_b, "LTIM")
    check("LTIM substring trap refused", False, "returned a match — would buy EMULTIMQ!")
except LookupError:
    check("LTIM substring trap refused (would have bought EMULTIMQ)", True)
try:
    K.resolve(_b, "SUSPENDED")
    check("non-EQ/BE series refused", False, "accepted SM series")
except LookupError:
    check("non-EQ/BE series refused", True)

print("\n=== 13. instrument tick threaded through pricing ===")
_pi = K.marketable_limit(33760, "BUY", 0.005, 5.0)
check("POWERINDIA 5.00 tick -> multiple of 5", _pi % 5 == 0, _pi)
check("default 0.05 tick would be rejected", K.marketable_limit(33760, "BUY", 0.005) % 5 != 0)
check("SELL prices below the mark", K.marketable_limit(1000, "SELL", 0.005, 0.05) < 1000)

print("\n=== 14. an empty account is a state, not an error ===")
class _Empty(StubKotak):
    def holdings(self):
        return {"stCode": 5203, "errMsg": "No holdings found for this client 15P56"}
check("empty holdings -> {} (first rebalance would have crashed)", K.holdings(_Empty()) == {})

print("\n=== 15. execute() resolves before sending ===")
from deployment.dualmom_live import runner as RUN
class _NoResolve(StubKotak):
    def quotes(self, instrument_tokens=None, **kw):
        """Kotak returns a LIST of rows (verified live 2026-09-10), and this SDK
        build REJECTS isIndex/quote_type. The old last_price() assumed a dict and
        raised AttributeError on every call, which silently disabled the stop."""
        if kw:
            raise TypeError(f"NeoAPI.quotes() got an unexpected keyword argument "
                            f"'{sorted(kw)[0]}'")
        px = {"2885": "1263.0000", "21954": "235.0900", "11821": "2687.0000"}
        out = []
        for t in (instrument_tokens or []):
            tok = str(t.get("instrument_token"))
            if tok in px:
                out.append({"exchange_token": tok, "ltp": px[tok],
                            "ohlc": {"close": px[tok]}})
        return out

    def search_scrip(self, exchange_segment=None, symbol=None):
        return {"data": []}
_run = {"plan": {"safe": True, "blocked": [],
                 "sells": [{"symbol": "TCS", "side": "SELL", "qty": 5, "mark": 3800.0}],
                 "buys": []}, "log": []}
_out = RUN.execute(_run, _NoResolve(), force=True)
check("unresolvable SELL halts (cannot exit a held position)",
      bool(_out["halted"]) and "cannot resolve" in _out["halted"], str(_out.get("halted"))[:60])
_run2 = {"plan": {"safe": True, "blocked": [], "sells": [],
                  "buys": [{"symbol": "LTIM", "side": "BUY", "qty": 1, "mark": 100.0}]},
         "log": []}
_out2 = RUN.execute(_run2, StubKotak(), force=True)
check("unresolvable BUY dropped, run continues",
      not _out2["halted"] and len(_out2.get("dropped_buys", [])) == 1,
      f"halted={_out2['halted']} dropped={_out2.get('dropped_buys')}")
_run3 = {"plan": {"safe": True, "blocked": [], "sells": [],
                  "buys": [{"symbol": "HFCL", "side": "BUY", "qty": 10, "mark": 244.24}]},
         "log": []}
_out3 = RUN.execute(_run3, StubKotak(), force=True)
_o3 = _out3["orders"][0]
check("BE name sent as HFCL-BE, not HFCL", _o3.get("trading_symbol") == "HFCL-BE",
      str(_o3.get("trading_symbol")))


print("\n=== 16. funded account must NOT read as empty (the 2026-09-10 bug) ===")
_funded = StubKotak(cash=1_000_000.0)
check("filtered limits() really does return zero (Kotak's actual behaviour)",
      float(_funded.limits(segment="CASH", exchange="NSE", product="CNC")["Net"]) == 0.0)
check("cash_available() still finds the Rs 10L",
      K.cash_available(_funded) == 1_000_000.0, K.cash_available(_funded))
class _AllZero(StubKotak):
    def limits(self, **kw):
        return {"Net": "0", "CollateralValue": "0", "stat": "Ok"}
check("a genuinely empty account still reads 0",
      K.cash_available(_AllZero()) == 0.0)

print("\n=== 17. last_prices() against the REAL quotes() shape ===")
_q = StubKotak()
_px = K.last_prices(_q, ["RELIANCE", "HFCL"])
check("quotes() LIST response parsed", _px.get("RELIANCE") == 1263.0, str(_px))
check("BE name priced too", _px.get("HFCL") == 235.09, str(_px))
check("single-symbol wrapper works", K.last_price(_q, "RELIANCE") == 1263.0)
try:
    K.last_price(_q, "POWERINDIA")          # no quote row for it
    check("a name with no mark RAISES (never a silent 0)", False, "returned a price")
except RuntimeError:
    check("a name with no mark RAISES (never a silent 0)", True)
check("isIndex/quote_type are NOT sent (SDK rejects them)",
      K.last_prices(_q, ["RELIANCE"]).get("RELIANCE") == 1263.0)
print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print(f"    FAILED: {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
