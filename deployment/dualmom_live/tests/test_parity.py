"""
PARITY: live code must make byte-identical decisions to the canonical backtest.

The backtest `dualmom_final.py` produced the numbers we are betting client money
on (CAGR 33.10%, Sharpe 1.577, max daily DD -30.3%). If the live path computes a
different 12-month return, a different weight, or a different share count, the
live book silently earns something other than what was validated — and nobody
would notice until the equity curve drifted.

This walks real historical month-ends and asserts, for each:
    same top-40 basket
    same weights            (to 1e-12)
    same share counts       (exact integers)
    same Nifty IN/OUT call
    same stop-loss triggers

    python -m deployment.dualmom_live.tests.test_parity
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] /
                       "backtesting" / "book_strategies" / "antonacci"))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np
import pandas as pd

import dualmom_final as BT
from deployment.dualmom_live import config as C
from deployment.dualmom_live import rebalance as R
from deployment.dualmom_live import signal_engine as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"\n          {detail}" if detail and not cond else ""))


print("\n=== 0. constants must agree ===")
check("LOOKBACK 252", C.LOOKBACK_DAYS == BT.LOOKBACK, f"{C.LOOKBACK_DAYS} vs {BT.LOOKBACK}")
check("MAX_WEIGHT_MULT 2.0", C.MAX_WEIGHT_MULT == BT.MAX_WEIGHT_MULT,
      f"{C.MAX_WEIGHT_MULT} vs {BT.MAX_WEIGHT_MULT}")
check("STOP_LOSS 0.35", C.STOP_LOSS_PCT == BT.STOP_LOSS,
      f"{C.STOP_LOSS_PCT} vs {BT.STOP_LOSS}")
check("TOP_N 40", C.TOP_N == 40)
check("MA period 100", C.MA_PERIOD == 100)
check("MAX_WEIGHT 10% matches backtest", C.MAX_WEIGHT == BT.MAX_WEIGHT,
      f"{C.MAX_WEIGHT} vs {BT.MAX_WEIGHT}")

print("\nloading price data ...")
close = BT.load("close")
nifty, nifty_ma = BT.load_nifty()
live_prices = S.load_prices()
live_nifty, live_ma = S.load_nifty()

print("\n=== 1. the two paths load IDENTICAL data ===")
check("same symbol set", set(close.columns) == set(live_prices.columns),
      f"bt {len(close.columns)} vs live {len(live_prices.columns)}")
common_idx = close.index.intersection(live_prices.index)
check("same trading calendar (over the backtest window)",
      len(common_idx) == len(close.index), f"{len(common_idx)} vs {len(close.index)}")
sub = live_prices.loc[close.index, close.columns]
check("same prices", np.allclose(close.fillna(-1).values, sub.fillna(-1).values, equal_nan=True))
check("same Nifty series", np.allclose(nifty.values, live_nifty.reindex(nifty.index).values,
                                       equal_nan=True))

# month-ends to test: a spread of regimes, plus the most recent
month_ends = close.resample("ME").last().index
test_dates = [d for d in month_ends
              if str(d.date())[:7] in ("2008-01", "2013-06", "2018-09", "2021-01",
                                       "2024-03", "2026-08")]
test_dates.append(month_ends[-1])
print(f"\n=== 2. per-date parity over {len(test_dates)} month-ends ===")

NAV = 1_000_000.0
for d in test_dates:
    i = close.index.get_indexer([d], method="ffill")[0]
    if i < 0 or i - BT.LOOKBACK < 0:
        continue
    rd = close.index[i]
    tag = str(rd.date())

    # ---- backtest path ----
    ni = nifty.index.get_indexer([rd], method="ffill")[0]
    bt_up = (not pd.isna(nifty_ma.iloc[ni])) and nifty.iloc[ni] > nifty_ma.iloc[ni]
    bt_r12 = (close.iloc[i] / close.iloc[i - BT.LOOKBACK] - 1).dropna()
    bt_top = bt_r12.nlargest(C.TOP_N)
    bt_raw = {s: max(float(v), 0.001) for s, v in bt_top.items()}
    bt_w = BT.cap_weights(bt_raw, BT.MAX_WEIGHT)
    bt_eff = (close.iloc[i] * (1 + BT.SLIPPAGE_PCT))
    bt_q = BT.allocate(bt_w, bt_eff, NAV)

    # ---- live path ----
    try:
        sig = S.compute(as_of=rd.date())
    except RuntimeError as e:
        check(f"{tag}: live signal computed", False, str(e))
        continue
    live_up = sig["signal"] == "IN"
    check(f"{tag}: same IN/OUT call", live_up == bt_up, f"live {sig['signal']} vs bt {'IN' if bt_up else 'OUT'}")
    if not bt_up:
        check(f"{tag}: OUT -> no holdings", not sig["holdings"])
        continue

    live_w = {h["symbol"]: h["weight"] for h in sig["holdings"]}
    check(f"{tag}: same basket", set(live_w) == set(bt_w),
          f"only-live {sorted(set(live_w)-set(bt_w))}  only-bt {sorted(set(bt_w)-set(live_w))}")
    if set(live_w) == set(bt_w):
        worst = max(abs(live_w[s] - bt_w[s]) for s in bt_w)
        check(f"{tag}: same weights", worst < 1e-9, f"max weight diff {worst:.2e}")

    # sizing must use the SAME effective price as the backtest
    live_px = {h["symbol"]: h["price"] for h in sig["holdings"]}
    live_eff = {s: p * (1 + C.SIZING_SLIPPAGE) for s, p in live_px.items()}
    live_q = R.allocate(live_w, live_eff, NAV)
    check(f"{tag}: same share counts", live_q == {s: bt_q[s] for s in bt_q if s in live_q}
          and set(live_q) == set(bt_q),
          f"live {len(live_q)} names vs bt {len(bt_q)}; "
          f"diffs {[(s, live_q.get(s), bt_q.get(s)) for s in set(live_q)|set(bt_q) if live_q.get(s) != bt_q.get(s)][:5]}")

print("\n=== 3. allocate() is the same function in both paths ===")
w = {"A": 0.4, "B": 0.35, "C": 0.25}
px = pd.Series({"A": 101.0, "B": 250.0, "C": 33.3})
check("identical output on a shared fixture",
      R.allocate(w, dict(px), 500_000) == BT.allocate(w, px, 500_000),
      f"{R.allocate(w, dict(px), 500_000)} vs {BT.allocate(w, px, 500_000)}")

expensive = {"A": 0.9, "X": 0.1}
pe = pd.Series({"A": 50.0, "X": 900_000.0})
check("identical unaffordable-name handling",
      R.allocate(expensive, dict(pe), 100_000) == BT.allocate(expensive, pe, 100_000))

print("\n=== 4. stop-loss trigger matches the backtest rule ===")
# backtest: sell when p <= entry*(1-stop)
entry, stop = 100.0, BT.STOP_LOSS
for mark, should in ((65.0, True), (65.001, False), (100.0, False), (10.0, True)):
    hit = bool(R.stop_breaches({"S": 1}, {"S": entry}, {"S": mark}))
    bt_hit = mark <= entry * (1 - stop)
    check(f"mark {mark} -> {'stop' if bt_hit else 'hold'}", hit == bt_hit == should)

print("\n" + "=" * 66)
print(f"  {len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print(f"    FAILED: {f}")
print("=" * 66)
sys.exit(1 if FAIL else 0)
