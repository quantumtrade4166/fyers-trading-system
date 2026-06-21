# ============================================================
# backtesting/test_indicators.py
#
# Smoke tests for all indicators in indicators.py
# Run: python backtesting/test_indicators.py
# ============================================================

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from backtesting.data_loader import DataLoader
from backtesting.indicators import (
    add_ema,
    add_rsi,
    add_vwap,
    add_supertrend,
    add_bollinger,
    apply_indicators,
)

SYMBOL = "NSE:RELIANCE-EQ"
WARMUP_BUFFER = 250  # bars to skip when checking warmup-dependent indicators


def separator(title: str):
    print(f"\n{'─' * 58}")
    print(f"  {title}")
    print(f"{'─' * 58}")


# ── Individual indicator tests ────────────────────────────────────────────────

def test_ema(df: pd.DataFrame):
    separator("TEST 1 — EMA (9, 20, 50, 200)")
    result = add_ema(df.copy(), period=9)
    result = add_ema(result, period=20)
    result = add_ema(result, period=50)
    result = add_ema(result, period=200)

    for p in [9, 20, 50, 200]:
        col = f"ema_{p}"
        assert col in result.columns, f"FAIL: column {col} not created"
        non_nan = result[col].dropna()
        assert len(non_nan) > 0, f"FAIL: {col} is all NaN"
        # EMA should be close to close price (same order of magnitude)
        assert (non_nan > 0).all(), f"FAIL: {col} contains non-positive values"

    r = result.iloc[WARMUP_BUFFER]
    print(f"  Sample bar (index {WARMUP_BUFFER}):")
    print(f"    close   = {r['close']:.2f}")
    print(f"    ema_9   = {r['ema_9']:.2f}")
    print(f"    ema_20  = {r['ema_20']:.2f}")
    print(f"    ema_50  = {r['ema_50']:.2f}")
    print(f"    ema_200 = {r['ema_200']:.2f}")

    # EMA relationship: faster EMA more reactive, so in uptrend ema_9 >= ema_200 typically
    # (can't assert direction, but can assert they're all positive finite values)
    assert all(
        np.isfinite(r[f"ema_{p}"]) for p in [9, 20, 50, 200]
    ), "FAIL: NaN or inf in EMA at sample bar"

    print("  PASS ✓")
    return result


def test_rsi(df: pd.DataFrame):
    separator("TEST 2 — RSI (14)")
    result = add_rsi(df.copy(), period=14)

    assert "rsi_14" in result.columns, "FAIL: rsi_14 column not created"

    rsi = result["rsi_14"].dropna()
    assert len(rsi) > 0, "FAIL: RSI is all NaN"
    assert rsi.between(0, 100).all(), f"FAIL: RSI values outside 0–100 range"

    # Check warmup NaN
    assert result["rsi_14"].iloc[:14].isna().all(), "FAIL: warmup rows should be NaN"

    sample = result["rsi_14"].dropna().iloc[50]
    print(f"  RSI range    : {rsi.min():.1f} – {rsi.max():.1f}")
    print(f"  RSI sample   : {sample:.2f}")
    print(f"  NaN count    : {result['rsi_14'].isna().sum()} (expected 14)")
    print("  PASS ✓")
    return result


def test_vwap(df: pd.DataFrame):
    separator("TEST 3 — VWAP (daily reset)")
    result = add_vwap(df.copy())

    assert "vwap" in result.columns, "FAIL: vwap column not created"

    vwap = result["vwap"]
    assert not vwap.isna().all(), "FAIL: VWAP is all NaN"
    assert (vwap > 0).all(), "FAIL: VWAP contains non-positive values"

    # VWAP should be between the day's low and high (roughly)
    # Check that VWAP is within 5% of close on average
    pct_diff = ((vwap - result["close"]) / result["close"]).abs()
    assert pct_diff.mean() < 0.05, f"FAIL: VWAP too far from close (avg {pct_diff.mean():.2%})"

    # Verify daily reset: VWAP at 09:15 should equal typical price at that bar
    first_bar_of_day = result.groupby(result.index.date).first()
    tp_first = (first_bar_of_day["high"] + first_bar_of_day["low"] + first_bar_of_day["close"]) / 3
    vwap_first = first_bar_of_day["vwap"]
    assert (tp_first - vwap_first).abs().max() < 0.01, \
        "FAIL: VWAP at first bar of day should equal typical price (daily reset)"

    print(f"  VWAP range   : {vwap.min():.2f} – {vwap.max():.2f}")
    print(f"  Avg |VWAP - close| / close : {pct_diff.mean():.2%}")
    print(f"  Daily reset  : verified (VWAP[09:15] == typical_price[09:15])")
    print("  PASS ✓")
    return result


def test_supertrend(df: pd.DataFrame):
    separator("TEST 4 — Supertrend (period=10, mult=3.0)")
    result = add_supertrend(df.copy(), period=10, multiplier=3.0)

    st_col  = "supertrend_10_3.0"
    dir_col = "supertrend_dir_10_3.0"

    assert st_col  in result.columns, f"FAIL: {st_col} not created"
    assert dir_col in result.columns, f"FAIL: {dir_col} not created"

    st  = result[st_col].dropna()
    dirs = result[dir_col]

    assert len(st) > 0, "FAIL: Supertrend is all NaN"
    assert set(dirs[dirs != 0].unique()).issubset({1, -1}), \
        "FAIL: direction values should be 1 or -1"

    # Supertrend should be a reasonable price level (positive, similar magnitude to close)
    assert (st > 0).all(), "FAIL: Supertrend contains non-positive values"

    up_count   = (dirs == 1).sum()
    down_count = (dirs == -1).sum()
    pct_up = up_count / (up_count + down_count) * 100

    print(f"  Uptrend bars   : {up_count:,} ({pct_up:.1f}%)")
    print(f"  Downtrend bars : {down_count:,} ({100-pct_up:.1f}%)")
    print(f"  NaN warmup bars: {result[st_col].isna().sum()}")

    # Sample
    r = result.iloc[WARMUP_BUFFER]
    trend_str = "UPTREND ↑" if r[dir_col] == 1 else "DOWNTREND ↓"
    print(f"  Sample bar: close={r['close']:.2f}  ST={r[st_col]:.2f}  dir={trend_str}")

    print("  PASS ✓")
    return result


def test_bollinger(df: pd.DataFrame):
    separator("TEST 5 — Bollinger Bands (period=20, std=2.0)")
    result = add_bollinger(df.copy(), period=20, std_dev=2.0)

    cols = ["bb_upper_20", "bb_middle_20", "bb_lower_20", "bb_width_20", "bb_pct_20"]
    for col in cols:
        assert col in result.columns, f"FAIL: {col} not created"

    # Basic relationships
    valid = result.dropna(subset=cols)
    assert (valid["bb_upper_20"] > valid["bb_middle_20"]).all(), \
        "FAIL: upper band should be above middle"
    assert (valid["bb_middle_20"] > valid["bb_lower_20"]).all(), \
        "FAIL: middle band should be above lower"
    assert (valid["bb_width_20"] > 0).all(), \
        "FAIL: band width should be positive"

    # %B: price at middle = 0.5, at upper = 1.0, at lower = 0.0 (approximately)
    bb_pct = valid["bb_pct_20"]
    print(f"  %B range    : {bb_pct.min():.3f} – {bb_pct.max():.3f}")
    print(f"  %B mean     : {bb_pct.mean():.3f} (expect ~0.5)")
    print(f"  Width range : {valid['bb_width_20'].min():.2f}% – {valid['bb_width_20'].max():.2f}%")

    r = valid.iloc[WARMUP_BUFFER]
    print(f"  Sample bar:")
    print(f"    close = {r['close']:.2f}")
    print(f"    upper = {r['bb_upper_20']:.2f}  middle = {r['bb_middle_20']:.2f}  lower = {r['bb_lower_20']:.2f}")
    print(f"    %B    = {r['bb_pct_20']:.3f}  width = {r['bb_width_20']:.2f}%")

    print("  PASS ✓")
    return result


def test_apply_indicators(df: pd.DataFrame):
    separator("TEST 6 — apply_indicators() batch config")
    t0 = time.perf_counter()
    result = apply_indicators(df.copy(), {
        "ema": [9, 21, 50],
        "rsi": [14],
        "vwap": True,
        "supertrend": {"period": 10, "multiplier": 3.0},
        "bollinger": {"period": 20, "std_dev": 2.0},
    })
    elapsed = time.perf_counter() - t0

    expected_cols = [
        "ema_9", "ema_21", "ema_50",
        "rsi_14",
        "vwap",
        "supertrend_10_3.0", "supertrend_dir_10_3.0",
        "bb_upper_20", "bb_middle_20", "bb_lower_20", "bb_width_20", "bb_pct_20",
    ]
    for col in expected_cols:
        assert col in result.columns, f"FAIL: {col} missing from apply_indicators output"

    print(f"  All {len(expected_cols)} columns created in {elapsed:.3f}s")
    print(f"  Final DataFrame shape: {result.shape}")
    print(f"  All columns: {[c for c in result.columns if c not in ['open','high','low','close','volume','symbol']]}")
    print("  PASS ✓")


def test_multiple_supertrend(df: pd.DataFrame):
    separator("TEST 7 — Multiple Supertrend configs simultaneously")
    result = apply_indicators(df.copy(), {
        "supertrend": [
            {"period": 7,  "multiplier": 3.0},
            {"period": 10, "multiplier": 3.0},
            {"period": 14, "multiplier": 2.0},
        ]
    })

    for col in ["supertrend_7_3.0", "supertrend_10_3.0", "supertrend_14_2.0"]:
        assert col in result.columns, f"FAIL: {col} not created"
    print(f"  Three Supertrend variants created simultaneously")
    print("  PASS ✓")


def test_no_lookahead(df: pd.DataFrame):
    separator("TEST 8 — No lookahead bias check")
    # If we compute EMA on the first N rows, it should equal the EMA
    # computed on the full dataset for those same rows.
    # True lookahead-free indicators satisfy this.
    n_rows = 500
    df_full  = add_ema(df.copy(), period=20)
    df_short = add_ema(df.iloc[:n_rows].copy(), period=20)

    diff = (df_full["ema_20"].iloc[:n_rows] - df_short["ema_20"]).abs()
    assert diff.max() < 1e-6, \
        f"FAIL: EMA differs between full/partial DataFrame — possible lookahead (max diff={diff.max():.2e})"

    print(f"  EMA on first {n_rows} bars matches full-series values")
    print("  PASS ✓")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 58)
    print("  Indicators Test Suite")
    print("=" * 58)

    print(f"\nLoading {SYMBOL}...")
    loader = DataLoader()
    df = loader.load(SYMBOL)
    print(f"  {len(df):,} bars loaded  ({df.index[0].date()} → {df.index[-1].date()})")

    passed, failed = 0, 0

    tests = [
        lambda: test_ema(df),
        lambda: test_rsi(df),
        lambda: test_vwap(df),
        lambda: test_supertrend(df),
        lambda: test_bollinger(df),
        lambda: test_apply_indicators(df),
        lambda: test_multiple_supertrend(df),
        lambda: test_no_lookahead(df),
    ]

    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            import traceback
            print(f"  ERROR: {exc}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'=' * 58}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 58}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
