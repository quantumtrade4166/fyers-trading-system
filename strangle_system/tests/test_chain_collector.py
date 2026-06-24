"""
Offline tests for chain_collector parsing logic.

No Fyers token / network needed — we feed a SYNTHETIC response shaped like the
documented Fyers v3 optionchain payload. Once `--probe` confirms the live key
names, update the fixture + parse_options_chain together and these stay green.
"""

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

from strangle_system.data import chain_collector as cc

IST = timezone(timedelta(hours=5, minutes=30))


def _synthetic_response():
    """Mimics greeks=1 optionchain: index row + CE/PE rows + expiryData + VIX."""
    return {
        "s": "ok",
        "code": 200,
        "data": {
            "indiavixData": {"ltp": 13.5},
            "expiryData": [
                {"date": "24-06-2026", "expiry": 1782547200},
                {"date": "01-07-2026", "expiry": 1783152000},
                {"date": "08-07-2026", "expiry": 1783756800},
            ],
            "optionsChain": [
                # index/underlying row (no CE/PE) → spot
                {"symbol": "NSE:NIFTY50-INDEX", "option_type": "", "ltp": 24000.0},
                {"symbol": "NSE:NIFTY...23900CE", "option_type": "CE", "strike_price": 23900,
                 "ltp": 180.0, "bid": 179.5, "ask": 180.5, "volume": 120000, "oi": 50000,
                 "prev_oi": 48000, "iv": 12.4, "delta": 0.62, "gamma": 0.0008,
                 "theta": -9.1, "vega": 6.2},
                {"symbol": "NSE:NIFTY...23900PE", "option_type": "PE", "strike_price": 23900,
                 "ltp": 70.0, "bid": 69.5, "ask": 70.5, "volume": 90000, "oi": 41000,
                 "prev_oi": 40000, "iv": 13.1, "delta": -0.38, "gamma": 0.0008,
                 "theta": -8.2, "vega": 6.0},
                {"symbol": "NSE:NIFTY...24100CE", "option_type": "CE", "strike_price": 24100,
                 "ltp": 95.0, "bid": 94.5, "ask": 95.5, "volume": 80000, "oi": 60000,
                 "prev_oi": 55000, "iv": 12.9, "delta": 0.41, "gamma": 0.0009,
                 "theta": -8.8, "vega": 6.4},
            ],
        },
    }


def test_extract_expiries_sorted_and_parsed():
    exps = cc._extract_expiries(_synthetic_response())
    assert len(exps) == 3
    dates = [d for d, _ in exps]
    assert str(dates[0]) == "2026-06-24"
    assert dates == sorted(dates)               # ascending
    assert all(ts is not None for _, ts in exps)  # epochs present


def test_parse_emits_only_option_rows_with_spot():
    cap = datetime(2026, 6, 24, 15, 25, tzinfo=IST)
    df = cc.parse_options_chain(_synthetic_response(), "NIFTY", None, cap)
    # 3 option rows (index row excluded)
    assert len(df) == 3
    assert set(df["opt_type"]) == {"CE", "PE"}
    # spot picked from index row and broadcast
    assert (df["spot"] == 24000.0).all()
    # india vix captured
    assert (df["india_vix"] == 13.5).all()


def test_greeks_and_oi_mapped():
    cap = datetime(2026, 6, 24, 15, 25, tzinfo=IST)
    df = cc.parse_options_chain(_synthetic_response(), "NIFTY", None, cap)
    ce = df[(df.opt_type == "CE") & (df.strike == 23900)].iloc[0]
    assert ce["oi"] == 50000 and ce["prev_oi"] == 48000
    assert abs(ce["iv"] - 12.4) < 1e-9
    assert abs(ce["delta"] - 0.62) < 1e-9
    assert ce["gamma"] > 0
    assert cc._greeks_present(df) is True
    # schema is exactly the canonical column set, in order
    assert list(df.columns) == cc.SNAPSHOT_COLUMNS


def test_failsafe_on_bad_response():
    # missing optionsChain → empty frame with correct schema, no crash
    df = cc.parse_options_chain({"s": "ok", "data": {}}, "NIFTY", None,
                                datetime.now(IST))
    assert df.empty
    assert list(df.columns) == cc.SNAPSHOT_COLUMNS


def test_nested_greeks_fallback():
    """If greeks arrive nested under 'greeks' instead of inline, still mapped."""
    resp = {"s": "ok", "data": {"optionsChain": [
        {"symbol": "X", "option_type": "", "ltp": 100.0},
        {"option_type": "CE", "strike_price": 100, "ltp": 5.0,
         "greeks": {"iv": 11.0, "gamma": 0.01, "delta": 0.5}},
    ]}}
    df = cc.parse_options_chain(resp, "NIFTY", None, datetime.now(IST))
    row = df.iloc[0]
    assert abs(row["iv"] - 11.0) < 1e-9
    assert abs(row["gamma"] - 0.01) < 1e-9


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
