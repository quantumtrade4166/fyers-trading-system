import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Engine tests — run:  .venv\\Scripts\\python.exe -m backtesting.engine.test_engine

1. Lookahead: features and EVERY book strategy give identical values for a
   bar whether or not later bars exist (truncate-and-compare).
2. Session square-off: force_exit closes at that bar's close, blocks entries.
3. Mirror symmetry: the short side is the long rule on a mirrored chart.
4. The old Kaufman import path still works.
"""

import importlib
import numpy as np
import pandas as pd

from backtesting.engine import harness, vpa, universe

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {detail}")


def _equal(a: pd.DataFrame, b: pd.DataFrame) -> bool:
    a, b = a.reset_index(drop=True), b.reset_index(drop=True)
    for col in a.columns:
        x, y = a[col], b[col]
        if x.dtype == object or y.dtype == object or x.dtype == bool:
            if not (x.fillna(False).astype(str) == y.fillna(False).astype(str)).all():
                return False
        elif not np.allclose(x.astype(float), y.astype(float), equal_nan=True):
            return False
    return True


def test_lookahead(folder="NSE_RELIANCE_EQ"):
    print("\n[1] lookahead — truncate and compare")
    df = universe.load_in_stock(folder).iloc[:6000]
    cut = 5000
    full = vpa.add_vpa_features(df, intraday=True)
    part = vpa.add_vpa_features(df.iloc[:cut], intraday=True)
    num = [c for c in full.columns if c != "day"]
    check("vpa features", _equal(full[num].iloc[:cut], part[num]))

    S = importlib.import_module("backtesting.book_strategies.coulling.strategies")
    cfgs = {"s11_9": {"harness": {"trailing": True}}, "s11_11": {"gap": 0.005},
            "s11_13": {"pattern": "triple"}}
    fm, pm = (vpa.add_vpa_features(vpa.mirror(df), True),
              vpa.add_vpa_features(vpa.mirror(df.iloc[:cut]), True))
    for name in [f"s11_{k}" for k in range(1, 15)]:
        fn = getattr(S, name)
        cfg = cfgs.get(name, {})
        a = universe.combine_sides(fn, full, fm, cfg).iloc[:cut]
        b = universe.combine_sides(fn, part, pm, cfg)
        # 11.14 builds each day's levels from EARLIER days only, but the last
        # day of the truncated frame may be partial — compare complete days.
        if name == "s11_14":
            last_day = part.index[-1].normalize()
            keep = part.index.normalize() < last_day
            a, b = a[keep], b[keep]
        check(f"{name} signals", _equal(a, b))


def test_session():
    print("\n[2] session square-off")
    idx = pd.date_range("2025-01-01 09:15", periods=6, freq="5min")
    df = pd.DataFrame({"open": [100, 101, 102, 103, 104, 105],
                       "high": [101, 102, 103, 104, 105, 106],
                       "low": [99, 100, 101, 102, 103, 104],
                       "close": [101, 102, 103, 104, 105, 106]}, index=idx, dtype=float)
    sig = harness.blank_signals(df)
    sig.loc[idx[0], "entry"] = 1
    sig["stop_long"] = 90.0
    sig.loc[idx[3], "entry"] = 1
    sig["force_exit"] = False
    sig.loc[idx[2], "force_exit"] = True
    tr = harness.run(df, sig, {"entry_on": "close", "capital": 1e5})
    check("closes at force bar close", len(tr) >= 1 and tr[0].exit_reason == "session"
          and tr[0].exit_price == 103.0, str([t.to_dict() for t in tr]))
    sig2 = sig.copy(); sig2["entry"] = 0; sig2.loc[idx[2], "entry"] = 1
    check("no entry on a force bar", harness.run(df, sig2, {"entry_on": "close"}) == [])
    sig3 = harness.blank_signals(df); sig3.loc[idx[0], "entry"] = 1; sig3["stop_long"] = 90.0
    old = harness.run(df, sig3, {"entry_on": "close"})
    check("no force_exit column = old behaviour", len(old) == 1 and old[0].exit_reason == "eod")


def test_mirror():
    print("\n[3] mirror symmetry")
    df = universe.load_in_stock("NSE_RELIANCE_EQ").iloc[:3000]
    f, fm = vpa.add_vpa_features(df, True), vpa.add_vpa_features(vpa.mirror(df), True)
    check("rv identical on mirror", np.allclose(f["rv"], fm["rv"], equal_nan=True))
    check("up <-> down", (f["up"] == fm["down"]).all())
    check("pivot high <-> pivot low", (f["ph"] == fm["pl"]).all())
    check("shooting star <-> hammer", (f["shooting_star"] == fm["hammer"]).all())
    S = importlib.import_module("backtesting.book_strategies.coulling.strategies")
    sig = universe.combine_sides(S.s11_2, f, fm, {})
    short_only = universe.combine_sides(S.s11_2, f, fm, {"sides": "short"})
    check("short side = mirrored long entries",
          ((sig["entry"] < 0) == (short_only["entry"] < 0)).all()
          and (short_only["entry"] <= 0).all())
    ok = (short_only.loc[short_only["entry"] < 0, "stop_short"]
          >= df.loc[short_only["entry"] < 0, "high"] - 1e-9).all()
    check("short stops sit above the signal bar high", bool(ok))


def test_shim():
    print("\n[4] Kaufman import path")
    from backtesting.book_strategies.kaufman.harness import backtest, run, blank_signals  # noqa
    check("kaufman.harness re-exports engine", run is harness.run)


if __name__ == "__main__":
    test_session(); test_mirror(); test_shim(); test_lookahead()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
