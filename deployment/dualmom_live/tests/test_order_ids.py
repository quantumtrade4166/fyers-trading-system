"""
Tests for the 2026-09-15 live failure: all 40 orders rejected on a repeated tag.
No broker, no network.

    python -m deployment.dualmom_live.tests.test_order_ids
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.stdout.reconfigure(encoding="utf-8")

from deployment.dualmom_live import config as C
from deployment.dualmom_live import kotak_equity as K
from deployment.dualmom_live import runner as RUN
import deployment.dualmom_live_api as API

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


class KotakLike:
    """Rejects a REPEATED tag exactly like Kotak did live ('error from core')."""
    def __init__(self):
        self.seen, self.n, self.book = set(), 0, {}

    def place_order(self, **kw):
        t = kw.get("tag")
        if t in self.seen:
            return {"stCode": 1009, "errMsg": "error from core"}
        self.seen.add(t)
        self.n += 1
        oid = f"26091500{self.n:07d}"
        self.book[oid] = {"nOrdNo": oid, "ordSt": "complete", "fldQty": kw["quantity"],
                          "avgPrc": kw["price"], "GuiOrdId": t, "ordModNo": t}
        return {"stat": "Ok", "nOrdNo": oid, "stCode": 200}

    def order_report(self):
        return {"data": list(self.book.values())}


print("\n=== 1. every order gets a unique tag ===")
tags = [K.unique_tag(C.ORDER_TAG) for _ in range(150)]
check("150 consecutive tags are all distinct", len(set(tags)) == 150, len(set(tags)))
check("tags share the prefix", all(t.startswith(C.ORDER_TAG) for t in tags))
check("tags are 13 chars (same length the strangle's tags run live on Kotak)",
      all(len(t) == 13 for t in tags), {len(t) for t in tags})

print("\n=== 2. the live failure, replayed ===")
b = KotakLike()
b.seen.add("dualmom")                          # the 1-share IDEA test used it first
res = [K.place_limit(b, f"S{i}-EQ", "BUY", 1, 100.0, tag=C.ORDER_TAG) for i in range(40)]
check("all 40 orders accepted (was 0/40 live)",
      all(r["status"] == "PLACED" for r in res), [r["status"] for r in res][:5])
check("each result records the tag it actually sent", all(r.get("tag") for r in res))

print("\n=== 3. strategy_orders finds ours by prefix, in GuiOrdId ===")
b.book["x"] = {"nOrdNo": "x", "GuiOrdId": "vwsk101500123", "fldQty": "65"}   # strangle
b.book["y"] = {"nOrdNo": "y", "GuiOrdId": "dualmom", "fldQty": "1"}          # old test tag
mine = K.strategy_orders(b, C.ORDER_TAG)
check("finds all 40 of ours", len(mine) == 40, len(mine))
check("ignores the strangle's tag and the old constant tag",
      not any(o["nOrdNo"] in ("x", "y") for o in mine))
check("'dmX' non-numeric suffix is not ours",
      not K.strategy_orders(type("R", (), {"order_report": lambda s: {"data": [
          {"nOrdNo": "z", "GuiOrdId": "dmoops"}]}})(), "dm"))

print("\n=== 4. the month is recorded only when EVERYTHING filled ===")
all_rejected = {"halted": None, "orders": [{"status": "REJECTED"}] * 40}
done, s = API.rebalance_complete(all_rejected)
check("40 rejected, no halt -> NOT done (was recorded as done live)", done is False, s)
mixed = {"halted": None, "orders": [{"status": "FILLED"}] * 39 + [{"status": "UNFILLED:open"}]}
check("39 filled + 1 unfilled -> NOT done", API.rebalance_complete(mixed)[0] is False)
check("by_status groups UNFILLED:open as UNFILLED",
      API.rebalance_complete(mixed)[1]["by_status"].get("UNFILLED") == 1)
ok = {"halted": None, "orders": [{"status": "FILLED"}] * 40}
check("40 filled -> done", API.rebalance_complete(ok)[0] is True)
check("halted -> NOT done even if the listed orders filled",
      API.rebalance_complete({"halted": "margin", "orders": [{"status": "FILLED"}]})[0] is False)

print("\n=== 5. stale preview is refused ===")
from datetime import datetime, timedelta
check("fresh preview age ~0 min",
      API._plan_age_minutes({"started": datetime.now().isoformat(timespec="seconds")}) < 1)
old = {"started": (datetime.now() - timedelta(minutes=45)).isoformat(timespec="seconds")}
check(f"45-min-old preview exceeds the {API.PREVIEW_MAX_AGE_MIN}-min limit",
      API._plan_age_minutes(old) > API.PREVIEW_MAX_AGE_MIN)
check("missing timestamp -> None (treated as stale)", API._plan_age_minutes({}) is None)

print("\n" + "=" * 62)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    FAILED: {f}")
print("=" * 62)
sys.exit(1 if FAIL else 0)
