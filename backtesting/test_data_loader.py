# ============================================================
# backtesting/test_data_loader.py
#
# Quick smoke-test for DataLoader.
# Run from PyCharm or terminal:
#   python backtesting/test_data_loader.py
#
# All tests print PASS / FAIL. No external test framework needed.
# ============================================================

import sys
import time
from pathlib import Path

# Fix Windows console encoding (same issue as pipeline)
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# Ensure project root is on the path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from backtesting.data_loader import DataLoader


def separator(title: str):
    print(f"\n{'─' * 55}")
    print(f"  {title}")
    print(f"{'─' * 55}")


def test_available_symbols(loader: DataLoader):
    separator("TEST 1 — available_symbols()")
    syms = loader.available_symbols()
    print(f"  Symbols found : {len(syms)}")
    print(f"  First 5       : {syms[:5]}")
    print(f"  Last 5        : {syms[-5:]}")
    assert len(syms) > 100, f"FAIL: expected 180+, got {len(syms)}"
    print("  PASS ✓")
    return syms


def test_load_single(loader: DataLoader, symbol: str):
    separator(f"TEST 2 — load('{symbol}') full history")
    t0 = time.perf_counter()
    df = loader.load(symbol)
    elapsed = time.perf_counter() - t0

    print(f"  Shape         : {df.shape}")
    print(f"  Date range    : {df.index[0].date()}  →  {df.index[-1].date()}")
    print(f"  Columns       : {df.columns.tolist()}")
    print(f"  Load time     : {elapsed:.3f}s")
    print(f"  First row:")
    print(f"    {df.iloc[0].to_dict()}")
    print(f"  Last row:")
    print(f"    {df.iloc[-1].to_dict()}")

    assert not df.empty, "FAIL: DataFrame is empty"
    expected_cols = {"open", "high", "low", "close", "volume", "symbol"}
    assert set(df.columns) == expected_cols, \
        f"FAIL: unexpected columns {df.columns.tolist()}"
    assert df.index.name == "datetime", "FAIL: index not named 'datetime'"
    assert df.index.is_monotonic_increasing, "FAIL: index not sorted ascending"
    print("  PASS ✓")
    return df


def test_load_date_range(loader: DataLoader, symbol: str):
    separator(f"TEST 3 — load() with date range")
    start, end = "2025-01-01", "2025-03-31"
    df = loader.load(symbol, start=start, end=end)

    print(f"  Period        : {start}  →  {end}")
    print(f"  Shape         : {df.shape}")
    print(f"  Actual range  : {df.index[0].date()}  →  {df.index[-1].date()}")

    assert df.index[0].date().isoformat() >= start, "FAIL: data starts before start date"
    assert df.index[-1].date().isoformat() <= end,  "FAIL: data ends after end date"
    assert not df.empty, "FAIL: empty result for Q1 2025"
    print("  PASS ✓")


def test_market_hours(loader: DataLoader, symbol: str):
    separator("TEST 4 — market hours filter (09:15 – 15:30)")
    df = loader.load(symbol, start="2025-06-01", end="2025-06-30")
    times = df.index.time
    import pandas as pd
    t_open  = pd.Timestamp("09:15").time()
    t_close = pd.Timestamp("15:30").time()

    before_open  = (times < t_open).sum()
    after_close  = (times > t_close).sum()
    print(f"  Bars pre-09:15  : {before_open}")
    print(f"  Bars post-15:30 : {after_close}")
    assert before_open == 0,  "FAIL: bars found before 09:15"
    assert after_close == 0,  "FAIL: bars found after 15:30"
    print("  PASS ✓")


def test_load_many(loader: DataLoader, symbols: list):
    separator("TEST 5 — load_many() batch load")
    batch = symbols[:5]
    print(f"  Loading: {batch}")
    t0 = time.perf_counter()
    data = loader.load_many(batch)
    elapsed = time.perf_counter() - t0

    print(f"  Loaded {len(data)}/{len(batch)} symbols in {elapsed:.3f}s")
    for sym, df in data.items():
        print(f"    {sym:<30} {df.shape[0]:>7,} bars")

    assert len(data) == len(batch), f"FAIL: expected {len(batch)}, got {len(data)}"
    print("  PASS ✓")


def test_missing_symbol(loader: DataLoader):
    separator("TEST 6 — missing symbol raises FileNotFoundError")
    try:
        loader.load("NSE:FAKESYMBOL-EQ")
        print("  FAIL: no exception raised")
    except FileNotFoundError as exc:
        print(f"  Caught FileNotFoundError (expected):")
        print(f"    {str(exc).splitlines()[0]}")
        print("  PASS ✓")


def test_symbol_info(loader: DataLoader, symbol: str):
    separator(f"TEST 7 — symbol_info('{symbol}')")
    info = loader.symbol_info(symbol)
    print(f"  Status        : {info.get('status')}")
    print(f"  Date range    : {info.get('date_from')}  →  {info.get('date_to')}")
    print(f"  Total bars    : {info.get('bars_total'):,}" if info.get('bars_total') else "  Total bars    : N/A")
    print(f"  Last checked  : {info.get('last_checked')}")
    print("  PASS ✓")


def test_cache(loader: DataLoader, symbol: str):
    separator("TEST 8 — cache speedup")
    t0 = time.perf_counter()
    loader.load(symbol)
    cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    loader.load(symbol)
    warm = time.perf_counter() - t0

    print(f"  Cold load : {cold:.3f}s")
    print(f"  Warm load : {warm:.4f}s  (cache hit)")
    assert warm < cold, "FAIL: cache did not speed up second load"
    print("  PASS ✓")


def test_summary(loader: DataLoader):
    separator("TEST 9 — summary() DataFrame")
    df = loader.summary()
    print(f"  Shape     : {df.shape}")
    print(f"  Columns   : {df.columns.tolist()}")
    if not df.empty:
        print(f"  Sample:")
        print(df.head(3).to_string(index=False))
    print("  PASS ✓")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  DataLoader Smoke Test")
    print("=" * 55)

    loader = DataLoader()
    test_symbol = "NSE:RELIANCE-EQ"

    passed = 0
    failed = 0

    tests = [
        lambda: test_available_symbols(loader),
        lambda: test_load_single(loader, test_symbol),
        lambda: test_load_date_range(loader, test_symbol),
        lambda: test_market_hours(loader, test_symbol),
        lambda: (lambda syms: test_load_many(loader, syms))(loader.available_symbols()),
        lambda: test_missing_symbol(loader),
        lambda: test_symbol_info(loader, test_symbol),
        lambda: test_cache(loader, test_symbol),
        lambda: test_summary(loader),
    ]

    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"  {exc}")
            failed += 1
        except Exception as exc:
            print(f"  ERROR: {exc}")
            failed += 1

    print(f"\n{'=' * 55}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 55}\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
