import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from datetime import date, timedelta
from typing import List

# Fyers month codes: 1-9 for Jan-Sep, O/N/D for Oct-Nov-Dec
MONTH_CODE = {
    1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
    7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D",
}

# NIFTY options: 50-point strike intervals
NIFTY_STRIKE_INTERVAL = 50


def is_monthly_expiry(d: date) -> bool:
    """True if d is the last Tuesday of its month (NIFTY monthly series).
    NSE shifted NIFTY weekly expiry from Thursday to Tuesday.
    """
    if d.weekday() != 1:   # 1 = Tuesday
        return False
    return (d + timedelta(weeks=1)).month != d.month


def make_nifty_symbol(expiry: date, strike: int, option_type: str) -> str:
    """
    Generate Fyers symbol string for a NIFTY index option.

    Format: NSE:NIFTY{YY}{M_CODE}{DD}{STRIKE}{TYPE}
    Same format for both weekly and monthly expiries — no suffix.

    Month codes: 1-9 for Jan-Sep, O=Oct, N=Nov, D=Dec
    Examples:
      NSE:NIFTY2660224500CE   (Jun 2, 2026, strike 24500 CE)
      NSE:NIFTY26O0224500PE   (Oct 2, 2026, strike 24500 PE)

    option_type: "CE" or "PE"
    """
    yy = str(expiry.year)[-2:]
    m  = MONTH_CODE[expiry.month]
    dd = str(expiry.day).zfill(2)
    return f"NSE:NIFTY{yy}{m}{dd}{strike}{option_type}"


def get_nifty_expiries(from_date: date, to_date: date) -> List[date]:
    """
    Return all NIFTY weekly expiry Tuesdays in [from_date, to_date].

    NSE shifted NIFTY weekly expiry from Thursday to Tuesday.
    Note: does not adjust for NSE trading holidays (holiday shifts expiry
    to Monday — handle manually if needed).
    """
    expiries: List[date] = []
    d = from_date
    while d.weekday() != 1:          # advance to first Tuesday
        d += timedelta(days=1)
    while d <= to_date:
        expiries.append(d)
        d += timedelta(weeks=1)
    return expiries


def get_strikes_around_atm(atm: int, n_strikes: int = 10,
                            interval: int = NIFTY_STRIKE_INTERVAL) -> List[int]:
    """
    Return 2*n_strikes+1 strike prices centred on ATM (rounded to nearest interval).

    Example: atm=24680, n_strikes=3 → [24550, 24600, 24650, 24700, 24750, 24800, 24850]
    """
    rounded_atm = round(atm / interval) * interval
    return [rounded_atm + (i - n_strikes) * interval
            for i in range(2 * n_strikes + 1)]


def get_contract_dates(expiry: date, days_before: int = 21) -> tuple[date, date]:
    """Return (start, end) of a contract's active trading window."""
    return expiry - timedelta(days=days_before), expiry
