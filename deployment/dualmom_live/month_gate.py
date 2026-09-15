"""
When should the monthly rebalance run? — decided on facts, not a calendar guess.

THE BUG THIS REPLACES (found live, 2026-09-14)
    The old 18:30 job asked "is today the final session of its month IN THE DATA?".
    That is correct for a backtest, where the data runs past month-end. Live, the
    data always ends TODAY (the 16:10 refresh just appended it), so today always
    looked like the last session of the month. It fired on Friday 11-Sep and queued
    a plan — empty only because the signal happened to be OUT. On an IN day it would
    have queued a full 40-name rebalance every single evening.

THE RULE NOW
    On any morning, if this calendar month has not been rebalanced yet:
      * the signal date is the LAST session of the PREVIOUS month present in the
        data — which is certainly complete by now, no holiday list needed
      * trade it on this, the first session of the new month
    Closest achievable to the backtest. The backtest fills AT the month-end close
    itself; live cannot know that close until the market has shut, so it fills at
    the next session's prices instead - an unavoidable one-session lag.

    Holidays are handled by asking for evidence of trading, not a hard-coded list:
    if NIFTY printed no 1-minute candle today, the exchange is closed and the gate
    says "retry".

GUARDS
    * stale data  — the signal date must be within STALE_DAYS of the month start,
      or a broken refresh would silently rank weeks-old momentum
    * late window — after RETRY_DAYS into the month it stops trying and says so
      loudly; a rebalance a fortnight late is a different strategy
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

STALE_DAYS = 6      # month-end close must be within this many days of the 1st
RETRY_DAYS = 10     # stop auto-retrying this many days into the month


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def decide(today: date, data_dates, last_done_month, market_open) -> dict:
    """Pure decision. `data_dates` = iterable of dates present in the price data.

    Returns {"action": "run" | "skip" | "retry" | "refuse", "signal_date", "reason"}.
    """
    key = month_key(today)
    if last_done_month == key:
        return {"action": "skip", "signal_date": None,
                "reason": f"already rebalanced for {key}"}

    month_start = today.replace(day=1)
    if (today - month_start).days >= RETRY_DAYS:
        return {"action": "refuse", "signal_date": None,
                "reason": (f"{key} was never rebalanced and it is now "
                           f"{(today - month_start).days} days into the month — "
                           f"too late to trade automatically; needs a human")}

    if market_open is None:
        return {"action": "retry", "signal_date": None,
                "reason": "could not determine whether the exchange is open — "
                          "will retry next session (investigate if this repeats)"}
    if not market_open:
        return {"action": "retry", "signal_date": None,
                "reason": "exchange is not trading today (holiday or closed) — "
                          "will retry next session"}

    prior = sorted(d for d in data_dates if d < month_start)
    if not prior:
        return {"action": "refuse", "signal_date": None,
                "reason": "no price data before this month"}
    signal_date = prior[-1]

    if (month_start - signal_date).days > STALE_DAYS:
        return {"action": "refuse", "signal_date": signal_date,
                "reason": (f"latest pre-month data is {signal_date} — "
                           f"{(month_start - signal_date).days} days before {month_start}. "
                           f"The data refresh is broken; refusing to rank stale momentum")}

    return {"action": "run", "signal_date": signal_date,
            "reason": f"first session of {key}; signal on month-end close {signal_date}"}


# ── state ────────────────────────────────────────────────────────────────────

def _state_path() -> Path:
    from deployment.dualmom_live import runner as RUN
    return RUN.STATE / "month_gate.json"


def load_state() -> dict:
    p = _state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_done(key: str, detail: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    s = load_state()
    s["last_done_month"] = key
    s.setdefault("history", []).append({"month": key, "at": datetime.now().isoformat(
        timespec="seconds"), **detail})
    s["history"] = s["history"][-24:]
    p.write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")


# ── is the exchange trading today? ───────────────────────────────────────────
#
# Verified the hard way on Mon 14-Sep-2026 (Ganesh Chaturthi, NSE closed):
#   * Fyers 1-minute candles for NIFTY50-INDEX:  14-Sep s=no_data, 0 candles;
#     Fri 11-Sep s=ok, 375 candles from 09:15.  Real trades or none - proven both
#     ways, so this is the PRIMARY test.
#   * Fyers market_status() is NOT usable: at 19:38 on the holiday it reported the
#     NSE capital-market NORMAL session as "POSTCLOSE_CLOSED", i.e. it follows the
#     clock, not the exchange calendar. An earlier draft relied on it and returned
#     "unknown" every time.
#   * Kotak quotes() lstup_time was a DATE STRING on the holiday
#     ("Fri Sep 11 2026 05:30:00 GMT+0530") but a bare number ("1789119910", not
#     unix time) on the evening of 10-Sep. The first version did int(float(...)),
#     raised, and returned False - right that day for the wrong reason. It is now
#     only a parsed-date FALLBACK.
#   An unreadable answer is UNKNOWN (None), never silently "closed".

import re as _re

_KOTAK_DATE = _re.compile(r"\b([A-Z][a-z]{2}) ([A-Z][a-z]{2}) (\d{1,2}) (\d{4})\b")


def _fyers_traded_on(fy, day: date):
    """True if NIFTY50 printed any 1-minute candle on `day`, False on s=no_data,
    None if Fyers could not be asked. Call after 09:16 on the day in question."""
    try:
        r = fy.history({"symbol": "NSE:NIFTY50-INDEX", "resolution": "1",
                        "date_format": "1", "range_from": day.isoformat(),
                        "range_to": day.isoformat(), "cont_flag": "1"})
    except Exception:
        return None
    if not isinstance(r, dict):
        return None
    if r.get("s") == "no_data":
        return False
    if r.get("s") != "ok":
        return None
    for c in r.get("candles") or []:
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            if datetime.fromtimestamp(int(c[0]), ist).date() == day:
                return True
        except Exception:
            continue
    return False


def _kotak_last_update_date(client, probe="RELIANCE"):
    """Date of the last exchange update on a liquid name, if Kotak's string parses."""
    from deployment.dualmom_live import kotak_equity as K
    try:
        info = K.resolve(client, probe)
        rows = client.quotes(instrument_tokens=[{"instrument_token": str(info["token"]),
                                                 "exchange_segment": K.SEGMENT}])
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("message") or []
        for r in rows or []:
            m = _KOTAK_DATE.search(str(r.get("lstup_time", ""))) if isinstance(r, dict) else None
            if m:
                return datetime.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)}",
                                         "%b %d %Y").date()
    except Exception:
        pass
    return None


def market_traded_today(client, today: date = None, fyers=None):
    """True = the exchange is trading today, False = closed, None = cannot tell.

    Call at/after 09:20. Fyers 1-minute candles are the primary source; Kotak's
    last-update date is the fallback. The gate retries on False AND on None.
    """
    today = today or date.today()
    if fyers is not None:
        v = _fyers_traded_on(fyers, today)
        if v is not None:
            return v
    d = _kotak_last_update_date(client) if client is not None else None
    if d is None:
        return None
    return d == today
