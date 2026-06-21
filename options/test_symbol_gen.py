import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

from options.symbol_gen import (
    make_nifty_symbol, is_monthly_expiry,
    get_nifty_expiries, get_strikes_around_atm,
)


def test_monthly_detection():
    # NIFTY now expires on Tuesdays. May 2026 Tuesdays: 5, 12, 19, 26 — last = 26 (monthly)
    assert is_monthly_expiry(date(2026, 5, 26)), "26 May 2026 must be monthly"
    assert not is_monthly_expiry(date(2026, 5, 19)), "19 May 2026 must be weekly"
    assert not is_monthly_expiry(date(2026, 5, 12)), "12 May 2026 must be weekly"
    assert not is_monthly_expiry(date(2026, 5, 5)),  " 5 May 2026 must be weekly"
    print("PASS: monthly expiry detection")


def test_symbol_format():
    # Format: NSE:NIFTY{YY}{M_CODE}{DD}{STRIKE}{TYPE}  — no suffix
    # Month codes: 1-9 for Jan-Sep, O=Oct, N=Nov, D=Dec

    # Weekly: 22 May 2025 (May = 5)
    sym = make_nifty_symbol(date(2025, 5, 22), 24950, "CE")
    assert sym == "NSE:NIFTY25522 24950CE".replace(" ", ""), f"Got: {sym}"
    assert sym == "NSE:NIFTY2552224950CE", f"Got: {sym}"

    # Monthly: 29 May 2025 — same format, just different date
    sym = make_nifty_symbol(date(2025, 5, 29), 25000, "PE")
    assert sym == "NSE:NIFTY2552925000PE", f"Got: {sym}"

    # June 2026 weekly (from symbol master sample): Jun 2, 2026
    sym = make_nifty_symbol(date(2026, 6, 2), 19150, "CE")
    assert sym == "NSE:NIFTY2660219150CE", f"Got: {sym}"

    # October edge case: Oct = 'O'
    sym = make_nifty_symbol(date(2025, 10, 2), 25000, "CE")
    assert sym == "NSE:NIFTY25O0225000CE", f"Got: {sym}"

    print("PASS: symbol format")
    print(f"  May weekly  : {make_nifty_symbol(date(2025, 5, 22), 24950, 'CE')}")
    print(f"  Jun monthly : {make_nifty_symbol(date(2026, 6, 26), 24500, 'CE')}")
    print(f"  Oct edge    : {make_nifty_symbol(date(2025, 10, 2), 25000, 'CE')}")


def test_expiry_dates():
    # NIFTY now expires on Tuesdays. May 2026 Tuesdays: 5, 12, 19, 26
    expiries = get_nifty_expiries(date(2026, 5, 1), date(2026, 5, 31))
    expected = [
        date(2026, 5, 5),
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
    ]
    assert expiries == expected, f"Got: {expiries}"
    print(f"PASS: May 2026 expiries (Tuesdays): {expiries}")


def test_strikes():
    strikes = get_strikes_around_atm(24680, n_strikes=3, interval=50)
    # ATM 24680 rounds to 24700; n_strikes=3 gives 7 strikes
    expected = [24550, 24600, 24650, 24700, 24750, 24800, 24850]
    assert strikes == expected, f"Got: {strikes}"
    print(f"PASS: strikes around 24680: {strikes}")


def test_live_format(fyers_client=None):
    """
    Optional live test: confirms the symbol format returns real data from Fyers.
    Run manually after confirming today's token is valid.

    Usage:
        from auth.fyers_auth import get_fyers_client
        from options.test_symbol_gen import test_live_format
        test_live_format(get_fyers_client())
    """
    if fyers_client is None:
        print("SKIP: live format test (pass a fyers_client to run)")
        return

    # Use a recent past weekly expiry — adjust if this has rolled off Fyers history
    test_expiry = date(2025, 5, 22)
    symbol = make_nifty_symbol(test_expiry, 24500, "CE")
    data = {
        "symbol":      symbol,
        "resolution":  "D",
        "date_format": "1",
        "range_from":  "2025-05-15",
        "range_to":    "2025-05-22",
        "cont_flag":   "1",
    }
    response = fyers_client.history(data=data)
    if response.get("s") == "ok":
        bars = len(response.get("candles", []))
        print(f"PASS: live API — {symbol} → {bars} daily bars")
    else:
        msg = response.get("message", response)
        print(f"FAIL: {symbol} rejected by Fyers API: {msg}")
        print("      Check make_nifty_symbol() format in symbol_gen.py")


if __name__ == "__main__":
    print("=== Options Symbol Generator Tests ===\n")
    test_monthly_detection()
    test_symbol_format()
    test_expiry_dates()
    test_strikes()
    print("\n=== All offline tests passed ===")
    print("\nTo verify format against live Fyers API:")
    print("  from auth.fyers_auth import get_fyers_client")
    print("  from options.test_symbol_gen import test_live_format")
    print("  test_live_format(get_fyers_client())")
