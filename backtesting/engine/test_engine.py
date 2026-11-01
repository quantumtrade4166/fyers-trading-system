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


def test_targets():
    print("\n[2b] take-profit, stop precedence, breached stop")
    idx = pd.date_range("2025-01-01 09:15", periods=5, freq="5min")
    df = pd.DataFrame({"open": [100, 100, 101, 103, 104], "high": [101, 102, 104, 105, 106],
                       "low": [99, 99.5, 100.5, 102, 103], "close": [100, 101, 103, 104, 105]},
                      index=idx, dtype=float)
    sig = harness.blank_signals(df); sig.loc[idx[0], "entry"] = 1; sig["stop_long"] = 98.0
    tr = harness.run(df, sig, {"entry_on": "close", "target_r": 2.0})
    check("2R target hit (entry 100, stop 98 -> 104)", tr[0].exit_reason == "target" and tr[0].exit_price == 104.0,
          str(tr[0].to_dict()))
    sig2 = sig.copy(); sig2["target_long"] = 102.0
    tr2 = harness.run(df, sig2, {"entry_on": "close", "target_r": 2.0})
    check("price target overrides 2R", tr2[0].exit_reason == "target" and tr2[0].exit_price == 102.0)
    both = pd.DataFrame({"open": [100, 100], "high": [100, 105], "low": [100, 97], "close": [100, 100]},
                        index=idx[:2], dtype=float)
    s3 = harness.blank_signals(both); s3.loc[idx[0], "entry"] = 1; s3["stop_long"] = 98.0
    tr3 = harness.run(both, s3, {"entry_on": "close", "target_r": 2.0})
    check("bar touching stop AND target books the stop", tr3[0].exit_reason == "stop")
    gap = pd.DataFrame({"open": [100, 97], "high": [100, 98], "low": [100, 96], "close": [100, 97]},
                       index=idx[:2], dtype=float)
    s4 = harness.blank_signals(gap); s4.loc[idx[0], "entry"] = 1; s4["stop_long"] = 98.0
    check("next-open through the stop is skipped when asked",
          harness.run(gap, s4, {"entry_on": "next_open", "skip_if_stop_breached": True}) == [])
    old = harness.run(df, sig, {"entry_on": "close"})
    check("no target settings = old behaviour (no target exit)", old[0].exit_reason == "eod")


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


def test_levels_lookahead():
    print("\n[5] book #1 levels + strategies — truncate and compare (NIFTY 5-min)")
    from backtesting.engine import levels
    S = importlib.import_module("backtesting.book_strategies.prabhu.strategies")
    df = universe.load_in_index("NSE_NIFTY50_INDEX").iloc[:9000]
    cut = 8000
    # cut at the END of a session so the last day is complete in both
    day = df.index.normalize()
    cut = int(np.where(day[:cut] != day[cut - 1])[0][-1]) + 1
    def build(x):
        f = levels.add_levels(vpa.add_vpa_features(x, True))
        fm = levels.add_levels(vpa.add_vpa_features(vpa.mirror(x), True))
        return f, fm
    f, fm = build(df); p, pm = build(df.iloc[:cut])
    num = [c for c in p.columns if c != "day"]
    check("levels columns", _equal(f[num].iloc[:cut], p[num]))
    for name in ["od", "star_reclaim", "cprbo", "zone_break", "orb", "dayhigh_break", "gap_fill",
                 "ma_trend", "ppt", "retrace", "double_test", "odr", "fc_break", "vcpr_rev",
                 "gap_reject", "cpr_rev", "extreme_rev", "zone_rev", "gapborder_rej", "fake_break"]:
        fn = getattr(S, name)
        for cfg in ({}, {"sl": "level", "tgt": "level", "daytype": "trend", "entry": 2},
                    {"dtrend": True, "pin": True, "or_min": 15, "tgt": "level", "tgt_ema": True}):
            a = universe.combine_sides(fn, f, fm, cfg).iloc[:cut]
            b = universe.combine_sides(fn, p, pm, cfg)
            check(f"{name} {cfg or 'default'}", _equal(a, b))


def test_shim():
    print("\n[4] Kaufman import path")
    from backtesting.book_strategies.kaufman.harness import backtest, run, blank_signals  # noqa
    check("kaufman.harness re-exports engine", run is harness.run)


if __name__ == "__main__":
    test_session(); test_targets(); test_mirror(); test_shim(); test_lookahead(); test_levels_lookahead()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
