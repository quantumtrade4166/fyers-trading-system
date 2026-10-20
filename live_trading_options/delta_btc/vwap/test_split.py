"""
vwap/test_split.py — offline tests for the split-leg book (paper + live against a fake
Delta). No network.

    .venv/Scripts/python.exe live_trading_options/delta_btc/vwap/test_split.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import tempfile
import datetime as dt

import vwap.split_book as sb
import vwap.leg_exec as le
from vwap.split_book import SplitVwapBook
from vwap.leg_exec import LiveExec, _round_tick
from live.delta_client import DeltaAPIError, order_result

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


D = dt.datetime
CFG = {"label": "ist live", "start": "09:30", "entry_cutoff": "16:25", "square_off": "17:10",
       "combined_threshold": 100, "contracts": 500, "max_entries": 4, "mtm_stop_usd": 35,
       "stop_basis": "price", "split_legs": True, "live_capable": True}
PARAMS = {"versions": {"ist_live": CFG}, "trigger_offset": 1.0, "candle_minutes": 5,
          "max_entry_slippage_pct": 5,
          "fees": {"taker_rate_notional": 0.0001, "premium_cap_rate": 0.035},
          "live_orders": {"leverage": 200, "entry_attempts": 3, "entry_buffer_pct": 1.0,
                          "exit_buffers_pct": [2, 5, 10, 25], "exit_market_fallback": True}}
CE_K, PE_K = 86000.0, 84000.0
STRIKES = [83000.0, 84000.0, 85000.0, 86000.0, 87000.0]


class FakeChain:
    def __init__(self):
        self.strikes = STRIKES
        self.spot = 85000.0
        self.contract_value = 0.001
        self.mark = {}
        for k in STRIKES:
            self.mark[(k, "CE")] = max(1.0, (87500 - k) / 30)
            self.mark[(k, "PE")] = max(1.0, (k - 82500) / 30)
        self.mark[(CE_K, "CE")] = 40.0
        self.mark[(PE_K, "PE")] = 40.0
        self.ask = {}
        self.bid = {}

    def is_ready(self):
        return True

    def contract(self, k, t):
        return {"symbol": self.symbol_for(k, t), "product_id": int(k) + (1 if t == "CE" else 2),
                "tick_size": 0.1, "contract_value": 0.001}

    def symbol_for(self, k, t):
        return f"{'C' if t == 'CE' else 'P'}-BTC-{int(k)}-031026"

    def book_fill(self, k, t, side, qty):
        m = self.mark[(k, t)]
        return {"price": round(m * (0.99 if side == "SELL" else 1.01), 4), "source": "l2"}

    def seconds_to_settlement(self, now):
        return 1000


fc = FakeChain()
gc = lambda code, fresh=False: fc
sb.api_candles = lambda sym, res, a, b: [{"time": a, "volume": 10}]
# select_pair walks out from ATM 85000: 86000/84000 combined 80 <= 100
BID_RATIO = {}
le.orderbook = lambda sym: {
    "buy": [{"price": str(round(fc.mark[_key(sym)] * BID_RATIO.get(_key(sym)[1], 0.99), 1)), "size": 100000}],
    "sell": [{"price": str(round(fc.mark[_key(sym)] * 1.01, 1)), "size": 100000}]}


def _key(sym):
    cp, _, k, _ = sym.split("-")
    return (float(k), "CE" if cp == "C" else "PE")


def setm(bk, ce, pe, now):
    fc.mark[(CE_K, "CE")] = ce
    fc.mark[(PE_K, "PE")] = pe
    bk.on_poll(now, gc)


def fire(bk, now):
    """Arm at the current combined and tick it."""
    ce, pe = fc.mark[(CE_K, "CE")], fc.mark[(PE_K, "PE")]
    bk.trigger.pending = {"trigger": round(ce + pe, 4), "signal": "x"}
    bk.on_sample(now, ce, pe, gc)


def new_book(tmp=None):
    tmp = tmp or Path(tempfile.mkdtemp())
    return SplitVwapBook("ist_live", PARAMS, tmp, log=lambda m: None), tmp


# ── helpers ───────────────────────────────────────────────────────────────
check("tick: sell rounds down", _round_tick(40.07, 0.1, "sell") == 40.0)
check("tick: buy rounds up", _round_tick(40.01, 0.1, "buy") == 40.1)
r = order_result({"id": 7, "size": 500, "unfilled_size": 200, "average_fill_price": "41.5",
                  "paid_commission": "0.7", "state": "cancelled"}, 500)
check("order_result partial", r["filled"] == 300 and r["price"] == 41.5 and r["fee"] == 0.7)

# ── PAPER: entry, split, kept-leg own VWAP, re-entry, square-off ─────────
bk, tmp = new_book()
setm(bk, 40, 40, D(2026, 10, 3, 9, 30, 1))
check("cycle started on 86000/84000", bk.active and bk.pair["ce"] == CE_K and bk.pair["pe"] == PE_K)
check("leg builders exist", bk.leg_builders and set(bk.leg_builders) == {"CE", "PE"})
check("starts in paper", bk.mode == "paper")
setm(bk, 40, 40, D(2026, 10, 3, 9, 31))
fire(bk, D(2026, 10, 3, 9, 32))
check("paper entry: both legs", bk.legs["CE"] and bk.legs["PE"] and bk.trigger.entries == 1)
check("leg size = 500", bk.legs["CE"]["qty"] == 500)

# BTC rallies: CE busts, PE collapses; combined closes above VWAP at the 09:35 candle
setm(bk, 70, 15, D(2026, 10, 3, 9, 34))
setm(bk, 70, 15, D(2026, 10, 3, 9, 35, 1))     # 09:30 candle = entry candle: held
setm(bk, 75, 12, D(2026, 10, 3, 9, 39))
setm(bk, 75, 12, D(2026, 10, 3, 9, 40, 1))     # 09:35 candle closes above VWAP -> split
check("split: CE bought back", bk.legs["CE"] is None)
check("split: PE kept", bk.legs["PE"] and bk.legs["PE"]["kept"])
check("split: trigger flat (can re-arm)", not bk.trigger.in_pos)
check("one leg trade recorded (CE, loss)", len(bk.trades) == 1 and bk.trades[0]["leg"] == "CE"
      and bk.trades[0]["gross_usd"] < 0)
check("CE loss priced at 500 contracts", abs(bk.trades[0]["gross_usd"] -
      (bk.trades[0]["entry_combined"] - bk.trades[0]["exit_combined"]) * 0.5) < 1e-6)

# re-entry while PE kept: sells only CE
fire(bk, D(2026, 10, 3, 9, 41))
check("re-entry sells only the missing leg", bk.legs["CE"] and bk.legs["PE"]
      and bk.legs["PE"]["n"] == 1 and bk.legs["CE"]["n"] == 2)
check("pair again: kept flag cleared", not bk.legs["PE"]["kept"])

# combined exit again -> now PE busts (BTC drops), CE kept
setm(bk, 30, 40, D(2026, 10, 3, 9, 44))
setm(bk, 30, 40, D(2026, 10, 3, 9, 45, 1))     # entry candle (09:40) held
setm(bk, 30, 60, D(2026, 10, 3, 9, 49))
setm(bk, 30, 60, D(2026, 10, 3, 9, 50, 1))
check("second split: PE out, CE kept", bk.legs["PE"] is None and bk.legs["CE"]["kept"])

# kept CE's own candle closes above its own VWAP -> bought back
for m in range(51, 55):
    setm(bk, 30 + (m - 50) * 8, 50, D(2026, 10, 3, 9, m))
setm(bk, 70, 50, D(2026, 10, 3, 9, 55, 1))
check("kept leg exits on its own VWAP", bk.legs["CE"] is None and bk.pos is None)
check("kept-leg trade flagged kept", bk.trades[-1]["leg"] == "CE" and bk.trades[-1]["kept"])

# restart resumes legs and leg candles
setm(bk, 40, 40, D(2026, 10, 3, 9, 56))
fire(bk, D(2026, 10, 3, 9, 57))
n_before = len(bk.trades)
bk2, _ = new_book(tmp)
setm(bk2, 40, 40, D(2026, 10, 3, 9, 58))
check("resume: legs restored", bk2.legs["CE"] and bk2.legs["PE"] and len(bk2.trades) == n_before)
check("resume: leg candles restored", bk2.leg_builders and len(bk2.leg_builders["CE"].closed) >= 5)
check("resume: in_pos restored", bk2.trigger.in_pos)

# square-off closes everything and ends the cycle
setm(bk2, 40, 40, D(2026, 10, 3, 17, 10, 1))
check("square-off flat + cycle ended", not bk2.active)
cyc = [json.loads(l) for l in (tmp / "data" / "vwap_results" / "cycles.jsonl").read_text().splitlines()]
check("cycle summary has price_pnl", "price_pnl" in cyc[-1] and cyc[-1]["mode"] == "paper")

# ── PAPER: price-only MTM stop at -35 ─────────────────────────────────────
bk3, _ = new_book()
setm(bk3, 40, 40, D(2026, 10, 3, 10, 0, 1))
setm(bk3, 40, 40, D(2026, 10, 3, 10, 6))
fire(bk3, D(2026, 10, 3, 10, 6, 1))
sold = bk3.legs["CE"]["sell"] + bk3.legs["PE"]["sell"]
fc.ask = {}
setm(bk3, 60, 40, D(2026, 10, 3, 10, 7))          # price P&L ~ -(100-79.2)*0.5 = -10.4
check("no stop at -10", bk3.legs["CE"] and not bk3.trigger.done)
check("stop watches price (marks), not asks/fees", abs(bk3.mtm() - (sold - 100) * 0.5) < 0.01)
setm(bk3, 100, 40, D(2026, 10, 3, 10, 8))         # -(140-79.2)*0.5 = -30.4
check("no stop at -30", not bk3.trigger.done)
setm(bk3, 110, 40, D(2026, 10, 3, 10, 9))         # -35.4: 1st poll -> pending only
check("stop waits for confirmation (1 poll)", not bk3.trigger.done and bk3.legs["CE"])
setm(bk3, 110, 40, D(2026, 10, 3, 10, 9, 2))
setm(bk3, 110, 40, D(2026, 10, 3, 10, 9, 4))      # 3rd consecutive poll -> stop
check("stop fires at -35 after 3 polls and flattens", bk3.trigger.done and bk3.pos is None
      and "MTM stop" in (bk3.stop_reason or ""))

# ── 19-Oct: a one-tick MARK glitch must not fire the stop ─────────────────
fc.__init__()
fc.bid = {}
bg19, _ = new_book()
setm(bg19, 40, 40, D(2026, 10, 19, 10, 0, 1))
setm(bg19, 40, 40, D(2026, 10, 19, 10, 6))
fire(bg19, D(2026, 10, 19, 10, 6, 1))
pe_k = (float(bg19.pair["pe"]), "PE")
fc.ask[pe_k] = 41.0; fc.bid[pe_k] = 39.0                # the real book
setm(bg19, 40, 400, D(2026, 10, 19, 10, 7))             # PE MARK glitches to 400 (book 39/41)
check("mark glitch clamped to the book (PE seen at 41, not 400)", bg19._mark("PE") == 41.0)
check("mark glitch does not fire the stop", not bg19.trigger.done and bg19.legs["PE"])
# a glitch that IS inside a wide book still needs 3 polls; one tick then normal -> cleared
fc.ask[pe_k] = 400.0
setm(bg19, 40, 400, D(2026, 10, 19, 10, 7, 2))          # 1 poll in breach
fc.ask[pe_k] = 41.0
setm(bg19, 40, 40, D(2026, 10, 19, 10, 7, 4))           # back to normal
check("single-poll breach clears, no stop", not bg19.trigger.done and bg19._stop_hits == 0)

# ── NET stop at -$80 including every charge (user rule 2026-10-20) ───────
PN = json.loads(json.dumps(PARAMS))
PN["versions"]["ist_live"].update(mtm_stop_usd=80, stop_basis="net")
fc.__init__(); fc.bid = {}
bn = SplitVwapBook("ist_live", PN, Path(tempfile.mkdtemp()), log=lambda m: None)
setm(bn, 40, 40, D(2026, 10, 20, 10, 0, 1))
setm(bn, 40, 40, D(2026, 10, 20, 10, 6))
fire(bn, D(2026, 10, 20, 10, 6, 1))
ce_k, pe_k = (float(bn.pair["ce"]), "CE"), (float(bn.pair["pe"]), "PE")
sold = bn.legs["CE"]["sell"] + bn.legs["PE"]["sell"]
# (500 contracts) price P&L about -35 (far above -80) but buying back at the wide ASK + fees -> NET below -80
fc.ask[ce_k] = 230.0; fc.bid[ce_k] = 100.0; fc.ask[pe_k] = 41.0; fc.bid[pe_k] = 39.0
for k in range(3):
    setm(bn, 110, 40, D(2026, 10, 20, 10, 7, 2 * k))
check("net stop counts spread + fees: price ~-70 but net <= -80 -> stop",
      bn.trigger.done and bn.pos is None and "NET stop" in (bn.stop_reason or ""))
check("net view includes exit fee estimate", abs((bn.realized()) - sum(t["net_usd"] for t in bn.trades)) < 1e-6)

# ── KILL from the control file ────────────────────────────────────────────
bk4, tmp4 = new_book()
setm(bk4, 40, 40, D(2026, 10, 3, 11, 0, 1))
setm(bk4, 40, 40, D(2026, 10, 3, 11, 6))
fire(bk4, D(2026, 10, 3, 11, 6, 1))
(tmp4 / "data" / "vwap_state" / "live_control_ist_live.json").write_text(
    json.dumps({"mode": "paper", "kill": True, "updated": dt.date.today().isoformat()}))
bk4._ctl_at = 0
setm(bk4, 40, 40, D(2026, 10, 3, 11, 7))
check("KILL flattens + stops", bk4.pos is None and bk4.trigger.done and bk4.status == "killed")


# ── LIVE against a fake Delta ─────────────────────────────────────────────
class FakeClient:
    def __init__(self):
        self.pos = {}            # product_id -> signed contracts
        self.orders = []
        self.fill_ratio = {}     # product_id -> fraction an IOC limit fills
        self.fail_market = False
        self.margin_fail = set()

    def positions(self):
        return dict(self.pos)

    lev = {}

    def get_leverage(self, pid):
        return self.lev.get(pid, 100.0)

    def set_leverage(self, pid, x):
        self.lev[pid] = float(x)
        return float(x)

    def place_order(self, pid, side, size, *, order_type="limit_order", limit_price=None,
                    tif="ioc", reduce_only=False, client_order_id=None):
        if pid in self.margin_fail and side == "sell":
            raise DeltaAPIError("insufficient", code="insufficient_margin")
        k = (float(pid - 1), "CE") if pid % 2 == 1 else (float(pid - 2), "PE")
        mark = fc.mark[k]
        ratio = self.fill_ratio.get(pid, 1.0) if order_type == "limit_order" else \
            (0.0 if self.fail_market else 1.0)
        filled = int(size * ratio)
        px = float(limit_price) if limit_price else mark * 1.02
        if order_type == "limit_order":
            if side == "sell" and px > mark * 0.995:
                filled = 0                       # priced above the bid: no fill
            if side == "buy" and px < mark * 1.005:
                filled = 0
        self.pos[pid] = self.pos.get(pid, 0) + (filled if side == "buy" else -filled)
        self.orders.append((side, pid, size, order_type, limit_price, filled, client_order_id))
        return {"id": len(self.orders), "size": size, "unfilled_size": size - filled,
                "average_fill_price": str(px) if filled else None,
                "paid_commission": str(round(0.035 * px * filled * 0.001, 6)),
                "state": "closed" if filled == size else "cancelled",
                "client_order_id": client_order_id}


def live_book(client):
    bk, tmp = new_book()
    bk._client = client
    bk.exec = LiveExec(client, PARAMS["live_orders"], log=lambda m: None)
    bk.mode = "live"
    bk._ctl_at = 1e18                 # no control file in tests: freeze control reads
    return bk, tmp


fc.__init__()
cl = FakeClient()
bl, tmpl = live_book(cl)
setm(bl, 40, 40, D(2026, 10, 3, 12, 0, 1))
setm(bl, 40, 40, D(2026, 10, 3, 12, 6))
fire(bl, D(2026, 10, 3, 12, 6, 1))
ce_pid, pe_pid = int(CE_K) + 1, int(PE_K) + 2
check("live entry: two IOC sells placed", [o[0] for o in cl.orders] == ["sell", "sell"]
      and all(o[3] == "limit_order" for o in cl.orders))
check("live entry: Delta short 500 each", cl.pos.get(ce_pid) == -500 and cl.pos.get(pe_pid) == -500)
check("live entry: book legs real", bl.legs["CE"]["qty"] == 500 and bl.legs["CE"]["mode"] == "live")
check("live entry: client ids tagged", all(o[6] and o[6].startswith("bvw") and len(o[6]) <= 32
                                           for o in cl.orders))
check("live fee from Delta", bl.legs["CE"]["sell_fee"] > 0)
check("leverage set to 200x on both legs before selling", cl.lev.get(ce_pid) == 200 and cl.lev.get(pe_pid) == 200)

# exit: limit IOCs fill nothing -> escalates to MARKET, never skipped
cl.fill_ratio[ce_pid] = 0.0
cl.orders.clear()
bl._close_leg(D(2026, 10, 3, 12, 8), fc, "CE", "test exit")
types = [o[3] for o in cl.orders]
check("exit retries limits then MARKET", types[:4] == ["limit_order"] * 4 and types[-1] == "market_order")
check("exit filled -> leg closed, Delta flat", bl.legs["CE"] is None and cl.pos.get(ce_pid) == 0)

# exit where even market fails: leg stays OPEN and pending, retried next poll
cl.fill_ratio[pe_pid] = 0.0
cl.fail_market = True
bl._close_leg(D(2026, 10, 3, 12, 9), fc, "PE", "test exit 2")
check("unfilled exit keeps the leg open + pending", bl.legs["PE"] and bl.legs["PE"]["exit_pending"])
cl.fail_market = False
cl.fill_ratio[pe_pid] = 1.0
setm(bl, 40, 40, D(2026, 10, 3, 12, 10))
check("pending exit completes on the next poll", bl.legs["PE"] is None and cl.pos.get(pe_pid) == 0)

# a buy never exceeds Delta's short (manual close / expiry): booked, no long opened
fire(bl, D(2026, 10, 3, 12, 11))
cl.pos[ce_pid] = 0                      # someone closed the CE by hand
cl.orders.clear()
bl._close_leg(D(2026, 10, 3, 12, 12), fc, "CE", "test")
check("broker-flat leg: no buy order sent", not any(o[0] == "buy" for o in cl.orders))
check("broker-flat leg closed in book", bl.legs["CE"] is None and cl.pos.get(ce_pid) == 0)
bl._close_leg(D(2026, 10, 3, 12, 12), fc, "PE", "test")

# half-filled entry: PE sells only 40% -> retries, then unwinds -> flat, no lone leg
cl.fill_ratio = {pe_pid: 0.0}
cl.orders.clear()
fire(bl, D(2026, 10, 3, 12, 13))
check("half entry unwound: flat on Delta", cl.pos.get(ce_pid, 0) == 0 and cl.pos.get(pe_pid, 0) == 0)
check("half entry unwound: flat in book", bl.legs["CE"] is None and bl.legs["PE"] is None)

def fresh(b):
    """Reset the day's unwind cap between scenarios on the same test book."""
    b.unwinds_today = 0
    b.trigger.done = False
    b.stop_reason = None


# part-fill: PE fills only 60% per try -> the entry is COMPLETED (fix 2026-10-16),
# not trimmed or unwound: 300 + 120 + 48 = 468 first, then the completion round
fresh(bl)
cl.fill_ratio = {pe_pid: 0.6}
n_tr = len(bl.trades)
fire(bl, D(2026, 10, 3, 12, 13, 30))
# (the fake fills int(60%) so the very last contract never fills: 499 -> trim 1)
q = bl.legs["PE"]["qty"] if bl.legs["PE"] else 0
check("part-fill: completed (>= 495 of 500), legs equal", bl.legs["CE"] and q >= 495
      and bl.legs["CE"]["qty"] == q)
check("part-fill: Delta short equal", cl.pos.get(ce_pid) == -q and cl.pos.get(pe_pid) == -q)
check("part-fill: at most a tiny trim, no full unwind",
      all(t["contracts"] <= 5 for t in bl.trades[n_tr:]))

check("entry slippage + latency recorded", bl.legs["CE"]["entry_mark"] is not None
      and bl.legs["CE"]["entry_ms"] is not None)
cl.fill_ratio = {}
bl._close_all(D(2026, 10, 3, 12, 13, 40), fc, "t")
check("re-flat after part-fill test", cl.pos.get(ce_pid) == 0 and cl.pos.get(pe_pid) == 0)
check("exit slippage recorded", bl.trades[-1].get("exit_mark") is not None
      and "exit_slippage" in bl.trades[-1])

# leg candles + own VWAP are in the dashboard payload
pl = bl._chart_payload(D(2026, 10, 3, 12, 14))
check("payload has CE + PE leg candles with VWAP", set(pl.get("leg_candles", {})) == {"CE", "PE"}
      and pl["leg_candles"]["CE"]["candles"] and "vwap" in pl["leg_candles"]["CE"]["candles"][-1])

fresh(bl)
# COMBINED pre-check (fix 2026-10-06): the 06-Oct case — one leg's bid is under its
# old per-leg share of the floor but the COMBINED book clears it -> both legs sold
bg, _ = live_book(FakeClient())
cg = bg._client
fc.__init__()
setm(bg, 40, 40, D(2026, 10, 6, 9, 30, 1))
setm(bg, 50, 20, D(2026, 10, 6, 9, 36))
BID_RATIO["PE"] = 0.90                                 # PE bid 18.0 vs mark 20
bg.trigger.pending = {"trigger": 69.0, "signal": "x"}  # floor 65.55 (+2% cushion 66.86 <= book 67.5); old PE share 18.7 > 18.0
bg.on_sample(D(2026, 10, 6, 9, 36, 1), 49.4, 19.6, gc)   # combined 69.0 touches the trigger
check("guard: combined book clears floor -> BOTH legs sold (old per-leg guard refused PE)",
      bg.legs["CE"] and bg.legs["PE"] and cg.pos.get(ce_pid) == -500 and cg.pos.get(pe_pid) == -500)
check("guard: no unwind trade booked", not any("unwind" in t["exit_reason"] for t in bg.trades))
bg._close_all(D(2026, 10, 6, 9, 37), fc, "t")
# combined book under the floor -> NOTHING sent, signal stays armed, no entry burned
cg.orders.clear()
n_before = bg.trigger.entries
bg.trigger.in_pos = False
bg.trigger.pending = {"trigger": 75.0, "signal": "y"}  # floor 71.25 > book 67.5
bg.on_sample(D(2026, 10, 6, 9, 38, 1), 50, 20, gc)
check("guard: combined below floor -> no order sent", not cg.orders)
check("guard: signal stays armed, entry not burned",
      bg.trigger.pending and bg.trigger.pending["trigger"] == 75.0 and bg.trigger.entries == n_before)
BID_RATIO.clear()

# fees match Delta's ledger: 3.5% of premium + 18% GST (2026-10-06: 0.875 + 0.1575 = 1.0325)
fc.spot = 85000.0
check("fee incl GST matches Delta ledger", abs(bl._fee_q(50.0, 500, fc) - 1.0325) < 1e-6)

# ── 15-Oct replays: book MOVES between the pre-check and the order ────────
BOOK_SEQ = {}
_ob_plain = le.orderbook


def _ob_seq(sym):
    k = _key(sym)
    q = BOOK_SEQ.get(k[1])
    if q:
        px = q.pop(0) if len(q) > 1 else q[0]
        return {"buy": [{"price": str(px), "size": 100000}],
                "sell": [{"price": str(round(px * 1.02, 1)), "size": 100000}]}
    return _ob_plain(sym)


le.orderbook = _ob_seq
# 09:40:28 — trigger 85.92 (floor 81.63). PE sold 57.0; the CE book was 27.1 at the
# check and 26.0 when its order went (old leg floor 26.55 -> CE refused -> PE unwound,
# −$7.83). Now: the CE miss is COMPLETED off the fresh book.
fc.__init__()
c15 = FakeClient()
b15, _ = live_book(c15)
setm(b15, 27.6, 57.6, D(2026, 10, 15, 9, 30, 1))
setm(b15, 27.6, 57.6, D(2026, 10, 15, 9, 36))
BOOK_SEQ.update({"CE": [27.1, 26.0], "PE": [57.6, 57.6]})
b15.trigger.pending = {"trigger": 85.9215, "signal": "x"}
b15.on_sample(D(2026, 10, 15, 9, 36, 28), 27.6, 57.6, gc)
check("15-Oct 09:40:28 replay: strangle COMPLETED, no unwind",
      b15.legs["CE"] and b15.legs["PE"] and c15.pos.get(ce_pid) == -500 and c15.pos.get(pe_pid) == -500
      and not any("unwind" in t["exit_reason"] for t in b15.trades))
check("15-Oct 09:40:28 replay: combined fill within trigger -10%",
      b15.legs["CE"]["sell"] + b15.legs["PE"]["sell"] >= 85.9215 * 0.90)
b15._close_all(D(2026, 10, 15, 9, 36, 40), fc, "t")

# 09:45:34 — trigger 84.02 (floor 79.82, needs 81.42 with the 2% cushion). Books CE 26.0
# + PE 53.5 = 79.5: under floor + cushion -> NOTHING is sent (was: PE sold then unwound
# −$7.50), the signal stays armed and no entry is used.
c15.orders.clear()
b15.trigger.in_pos = False
e0 = b15.trigger.entries
BOOK_SEQ.update({"CE": [26.0], "PE": [53.5]})
b15.trigger.pending = {"trigger": 84.0158, "signal": "y"}
b15.on_sample(D(2026, 10, 15, 9, 37), 26.5, 54.0, gc)
check("15-Oct 09:45:34 replay: books too close to the floor -> nothing sent", not c15.orders)
check("15-Oct 09:45:34 replay: still armed, no entry used",
      b15.trigger.pending and b15.trigger.entries == e0)

# 09:39:56 — trigger 87.03 (floor 82.68, needs 84.33). PE book 56.0 + CE 27.0 = 83.0
# -> nothing sent (was: CE sold then unwound −$4.31).
BOOK_SEQ.update({"CE": [27.0], "PE": [56.0]})
b15.trigger.pending = {"trigger": 87.0273, "signal": "z"}
b15.on_sample(D(2026, 10, 15, 9, 37, 30), 27.5, 56.5, gc)
check("15-Oct 09:39:56 replay: nothing sent, no unwind", not c15.orders)

# a leg that truly cannot fill (book vanishes) is still unwound — never a lone leg
c16 = FakeClient(); c16.fill_ratio = {pe_pid: 0.0}
b16, _ = live_book(c16)
fc.__init__()
BOOK_SEQ.clear()
setm(b16, 40, 40, D(2026, 10, 16, 9, 30, 1))
setm(b16, 40, 40, D(2026, 10, 16, 9, 36))
fire(b16, D(2026, 10, 16, 9, 36, 1))
check("unfillable leg: completion tried, then unwound to flat",
      c16.pos.get(ce_pid, 0) == 0 and c16.pos.get(pe_pid, 0) == 0
      and b16.legs["CE"] is None and b16.legs["PE"] is None
      and sum(1 for o in c16.orders if o[0] == "sell" and o[1] == pe_pid) > 3)
check("unwind counted", b16.unwinds_today == 1 and not b16.trigger.done)
# jumpy day: a 2nd failed entry hits the cap -> no more entries today
b16.trigger.in_pos = False
fire(b16, D(2026, 10, 16, 9, 37))
check("2 unwinds -> entries stopped for the day", b16.unwinds_today == 2 and b16.trigger.done
      and "unwinds today" in (b16.stop_reason or ""))
n_ord = len(c16.orders)
b16.trigger.pending = {"trigger": 80.0, "signal": "w"}
b16.on_sample(D(2026, 10, 16, 9, 38), 40, 40, gc)
check("after the cap: no further orders", len(c16.orders) == n_ord)
le.orderbook = _ob_plain

# margin refusal stops entries for the day
fresh(bl)
cl.fill_ratio = {}
cl.margin_fail = {ce_pid}
fire(bl, D(2026, 10, 3, 12, 14))
check("margin refusal -> margin halt, no position", bl.margin_halt and bl.trigger.done
      and bl.pos is None and cl.pos.get(pe_pid, 0) == 0)

# restart in live with open legs: reconciled against Delta
cl2 = FakeClient()
bl2, tmp2 = live_book(cl2)
setm(bl2, 40, 40, D(2026, 10, 3, 13, 0, 1))
setm(bl2, 40, 40, D(2026, 10, 3, 13, 6))
fire(bl2, D(2026, 10, 3, 13, 6, 1))
bl2.write_state(D(2026, 10, 3, 13, 6, 2))
cl2.pos[pe_pid] = -200                  # Delta holds less PE than the book thinks
import live.delta_client as dc
orig = dc.DeltaClient
dc.DeltaClient = lambda *a, **k: cl2
bl3, _ = new_book(tmp2)
bl3._ctl_at = 1e18
setm(bl3, 40, 40, D(2026, 10, 3, 13, 7))
dc.DeltaClient = orig
check("restart comes back LIVE", bl3.mode == "live")
check("restart reconciles PE down to Delta's 200", bl3.legs["PE"] and bl3.legs["PE"]["qty"] == 200)
check("restart keeps CE 500", bl3.legs["CE"] and bl3.legs["CE"]["qty"] == 500)

# 1.5x VWAP cap: the busted leg goes the moment combined >= 1.5 x VWAP
PC = json.loads(json.dumps(PARAMS)); PC["versions"]["ist_live"]["vwap_cap_mult"] = 1.5
PC["versions"]["ist_live"]["live_capable"] = False
bc = SplitVwapBook("ist_live", PC, Path(tempfile.mkdtemp()), log=lambda m: None)
fc.__init__()
setm(bc, 40, 40, D(2026, 10, 3, 15, 0, 1))
setm(bc, 40, 40, D(2026, 10, 3, 15, 6))          # late start: first candle is 15:05
setm(bc, 40, 40, D(2026, 10, 3, 15, 10, 1))      # 15:05 candle closes -> VWAP 80
fire(bc, D(2026, 10, 3, 15, 10, 2))
check("cap: VWAP is 80", abs((bc.builder.live_vwap() or 0) - 80) < 0.01)
setm(bc, 70, 40, D(2026, 10, 3, 15, 11))         # 110 < 1.5 x 80 = 120: nothing
check("cap: no exit below 1.5x VWAP", bc.legs["CE"] and bc.legs["PE"])
setm(bc, 85, 40, D(2026, 10, 3, 15, 11, 30))     # 125 >= 120: split NOW, mid-candle
check("cap: busted CE out mid-candle at 1.5x VWAP", bc.legs["CE"] is None and bc.legs["PE"]
      and bc.legs["PE"]["kept"] and not bc.trigger.in_pos)
check("cap: reason says 1.5x", "1.5x VWAP" in bc.trades[-1]["exit_reason"])
bc0 = SplitVwapBook("ist_live", PARAMS, Path(tempfile.mkdtemp()), log=lambda m: None)
check("no cap configured -> no cap", bc0.vwap_cap == 0)

# a paper-reference version ignores an arm request
PP = json.loads(json.dumps(PARAMS)); PP["versions"]["ist_live"]["live_capable"] = False
bkp = SplitVwapBook("ist_live", PP, Path(tempfile.mkdtemp()), log=lambda m: None)
(bkp.state_dir / "live_control_ist_live.json").write_text(json.dumps({"mode": "live"}))
dc.DeltaClient = lambda *a, **k: FakeClient()
setm(bkp, 40, 40, D(2026, 10, 3, 14, 0, 1))
check("paper reference never goes live", bkp.mode == "paper" and not bkp.live_capable)
dc.DeltaClient = orig

# ── COPY PAPER -> LIVE (adopt) ────────────────────────────────────────────
def adopt_book():
    """A live-capable book holding a PAPER strangle, then armed with an adopt request."""
    fc.__init__()
    b, tmpa = new_book()
    setm(b, 40, 40, D(2026, 10, 13, 10, 0, 1))
    setm(b, 40, 40, D(2026, 10, 13, 10, 6))
    fire(b, D(2026, 10, 13, 10, 6, 1))                 # paper entry, both legs
    return b, tmpa


def press_adopt(b, tmpa, when, client):
    (tmpa / "data" / "vwap_state" / "live_control_ist_live.json").write_text(json.dumps(
        {"mode": "live", "kill": False, "adopt": dt.date.today().isoformat() + "T10:07:00",
         "updated": dt.date.today().isoformat()}))
    dc.DeltaClient = lambda *a, **k: client
    b._ctl_at = 0
    setm(b, 40, 40, when)
    dc.DeltaClient = orig


ba, ta = adopt_book()
check("adopt: paper position before", ba.mode == "paper" and ba.legs["CE"] and ba.legs["PE"])
paper_sell = (ba.legs["CE"]["sell"], ba.legs["PE"]["sell"])
ca = FakeClient()
press_adopt(ba, ta, D(2026, 10, 13, 10, 7), ca)
check("adopt: book is LIVE", ba.mode == "live" and ba.exec.live)
check("adopt: both legs sold on Delta at the paper size", ca.pos.get(ce_pid) == -500 and ca.pos.get(pe_pid) == -500)
check("adopt: legs carry the real fills + mode live", ba.legs["CE"]["mode"] == "live"
      and ba.legs["CE"]["adopted_from_paper"] == paper_sell[0] and ba.legs["CE"]["orders"])
check("adopt: entry count unchanged (strategy continues)", ba.trigger.entries == 1)
check("adopt: today's live P&L starts fresh, paper legs archived",
      ba.trades == [] and len(ba.paper_before_adopt) == 2
      and all("copied to LIVE" in t["exit_reason"] for t in ba.paper_before_adopt))
n_orders = len(ca.orders)
setm(ba, 40, 40, D(2026, 10, 13, 10, 8))
check("adopt: one press = one copy (no repeat)", len(ca.orders) == n_orders)
ba._close_all(D(2026, 10, 13, 10, 9), fc, "t")
check("adopt: later exits go to Delta", ca.pos.get(ce_pid) == 0 and ca.pos.get(pe_pid) == 0)

# single KEPT leg in paper -> only that leg is copied, kept state carried over
bk1, tk1 = adopt_book()
bk1._exit_signal(1, "close above VWAP")                # paper split: one leg kept
kept = [t for t in ("CE", "PE") if bk1.legs[t]]
check("adopt(kept): paper has one kept leg", len(kept) == 1 and bk1.legs[kept[0]]["kept"])
ck = FakeClient()
press_adopt(bk1, tk1, D(2026, 10, 13, 10, 7), ck)
pid_k = ce_pid if kept[0] == "CE" else pe_pid
check("adopt(kept): only the kept leg sold live", ck.pos.get(pid_k) == -500 and len(ck.pos) == 1)
check("adopt(kept): still a kept leg, now live", bk1.legs[kept[0]]["kept"] and bk1.legs[kept[0]]["mode"] == "live")

# PE fills only 60% per try -> equalized, both legs equal and live
bp, tp = adopt_book()
cp = FakeClient(); cp.fill_ratio = {pe_pid: 0.6}
press_adopt(bp, tp, D(2026, 10, 13, 10, 7), cp)
q = bp.legs["PE"]["qty"] if bp.legs["PE"] else None
check("adopt(part): equal live legs", bp.mode == "live" and bp.legs["CE"]["qty"] == q
      and cp.pos.get(ce_pid) == -q and cp.pos.get(pe_pid) == -q)

# PE fills nothing -> CE bought back, book stays PAPER with its paper legs
bf, tf = adopt_book()
cf = FakeClient(); cf.fill_ratio = {pe_pid: 0.0}
press_adopt(bf, tf, D(2026, 10, 13, 10, 7), cf)
check("adopt(fail): Delta flat again", cf.pos.get(ce_pid, 0) == 0 and cf.pos.get(pe_pid, 0) == 0)
check("adopt(fail): still paper with the paper legs", bf.mode == "paper"
      and bf.legs["CE"] and bf.legs["CE"]["mode"] == "paper")

# a request from an earlier day is ignored
bo, to = adopt_book()
co = FakeClient()
(to / "data" / "vwap_state" / "live_control_ist_live.json").write_text(json.dumps(
    {"mode": "live", "adopt": "2026-01-01T10:00:00"}))
dc.DeltaClient = lambda *a, **k: co
bo._ctl_at = 0
setm(bo, 40, 40, D(2026, 10, 13, 10, 7))
dc.DeltaClient = orig
check("adopt: stale request ignored", not co.orders and bo.mode == "paper")

# control file arms live only while flat
bk5, tmp5 = new_book()
setm(bk5, 40, 40, D(2026, 10, 3, 14, 0, 1))
setm(bk5, 40, 40, D(2026, 10, 3, 14, 6))
fire(bk5, D(2026, 10, 3, 14, 6, 1))
(tmp5 / "data" / "vwap_state" / "live_control_ist_live.json").write_text(
    json.dumps({"mode": "live", "kill": False, "updated": dt.date.today().isoformat()}))
dc.DeltaClient = lambda *a, **k: FakeClient()
bk5._ctl_at = 0
setm(bk5, 40, 40, D(2026, 10, 3, 14, 7))
check("no mode switch with a paper position open", bk5.mode == "paper")
bk5._close_all(D(2026, 10, 3, 14, 8), fc, "t")
bk5._ctl_at = 0
setm(bk5, 40, 40, D(2026, 10, 3, 14, 9))
check("switches to live once flat", bk5.mode == "live")
dc.DeltaClient = orig

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
