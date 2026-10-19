"""After-market backtest of the paper engine's day -> paper vs backtest per day.

Runs the backtest (backtesting/options_st_directional) on ICICI Breeze data for
each date with the SAME settings the paper engine used that day, and stores the
result next to the paper book so the dashboard can chart them side by side.

    .venv\\Scripts\\python.exe -m live_trading_options.nifty_st_directional.daily_backtest            # today
    .venv\\Scripts\\python.exe -m live_trading_options.nifty_st_directional.daily_backtest --dates 2026-10-12 2026-10-13

Before backtesting it makes sure the data exists:
  * NIFTY index 1-min for the date (options.breeze.fetch_index_spot)
  * every NIFTY expiry Kite currently lists is in the expiry calendar (public
    instrument dump, no login) -- the 2-sessions rule may pick a contract
    expiring after the last date the calendar knows

Output: data/results/backtest_daily.json  {date: {net, gross, charges, trades, ...}}
VPS task: DataStDirBacktest, Mon-Fri 16:30 IST.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))

from backtesting.options_st_directional.config import Config            # noqa: E402
from backtesting.options_st_directional import data as BD                # noqa: E402
from backtesting.options_st_directional.engine import run_day            # noqa: E402
from backtesting.options_st_directional import report                    # noqa: E402

PARAMS = json.loads((ROOT / "config" / "parameters.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "results" / "backtest_daily.json"
CALENDAR = REPO / "data" / "NSE_NIFTY_OPTIONS" / "expiry_calendar.csv"
BREAKEVEN_FROM = "2026-10-13"      # the paper engine got stop-to-cost (25) from this day
CPR_FROM = "2026-10-19"            # ...and the CPR width filter from this day
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def log(msg):
    print(f"[{dt.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


WARMUP_DAYS = 30                   # calendar days of index history checked before each date


def ensure_index(dates):
    """The date AND the 30 days before it: a gap in the warm-up silently changes the
    spot Supertrend (16-Oct: VPS file missing 12-Sep..09-Oct -> wrong 12-Oct trade).
    Missing weekdays are re-requested; a holiday just returns no bars."""
    have = set()
    if BD.INDEX_PATH.exists():
        ix = pd.read_parquet(BD.INDEX_PATH, columns=["datetime"])
        have = set(pd.to_datetime(ix["datetime"]).dt.strftime("%Y-%m-%d"))
    want = set()
    for d in dates:
        start = pd.Timestamp(d) - pd.Timedelta(days=WARMUP_DAYS)
        want |= {x.strftime("%Y-%m-%d") for x in pd.bdate_range(start, d)}
    missing = sorted(want - have)
    if not missing:
        return
    log(f"index 1-min: {len(missing)} weekday(s) missing {missing[0]}..{missing[-1]} -- downloading")
    subprocess.run([sys.executable, "-m", "options.breeze.fetch_index_spot",
                    "--start", missing[0], "--end", missing[-1]], cwd=str(REPO), check=False)


def sync_expiries():
    """Append every NIFTY expiry Kite lists (>= today) that the calendar lacks."""
    try:
        from kiteconnect import KiteConnect
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_kx", REPO / "live_trading_options/strangle_strategy/live/kite_executor.py")
        kx = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(kx)
        rows = KiteConnect(api_key=kx._API_KEY).instruments("NFO")
        listed = sorted({str(r["expiry"]) for r in rows
                         if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")})
    except Exception as e:
        log(f"WARN could not read Kite expiries: {e}")
        return
    cal = pd.read_csv(CALENDAR)
    new = [e for e in listed if e not in set(cal["expiry_date"])]
    if new:
        shutil.copy(CALENDAR, str(CALENDAR) + ".bak")
        cal = pd.concat([cal, pd.DataFrame({"expiry_date": new})]).drop_duplicates()
        cal.sort_values("expiry_date").to_csv(CALENDAR, index=False)
        log(f"expiry calendar: added {new}")


def backtest(dates):
    BD.FUTURE_HOLIDAYS = set(BD.FUTURE_HOLIDAYS) | set(PARAMS.get("nse_holidays", []))
    out = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    for d in dates:
        be = PARAMS.get("breakeven_after_points") if d >= BREAKEVEN_FROM else None
        cpr_max = PARAMS.get("cpr_max_width") if d >= CPR_FROM else None
        cfg = Config(start=d, end=d, lots=PARAMS["lots"], capital=PARAMS["capital"],
                     breakeven_after_points=be, cpr_max_width=cpr_max, tag="DAILY")
        prov = BD.BreezeProvider(cfg, verbose=False)
        if d not in prov.days_in_range(d, d):
            log(f"{d}: not a trading day in the index data -- skipped")
            continue
        r = run_day(d, prov, cfg)
        tr = pd.DataFrame(r["trades"])
        if not tr.empty:
            tr = report.add_pnl(tr[tr["exit_price"].notna()], cfg)
        row = {
            "date": d, "breakeven": be,
            "trades": int(len(tr)),
            "gross": round(float(tr["gross_pnl"].sum()), 2) if len(tr) else 0.0,
            "charges": round(float(tr["cost_total"].sum()), 2) if len(tr) else 0.0,
            "net": round(float(tr["net_pnl"].sum()), 2) if len(tr) else 0.0,
            "detail": [{"opt": t.opt_type, "strike": int(t.strike), "expiry": str(t.expiry),
                        "entry": str(t.entry_ts)[11:19], "entry_price": float(t.entry_price),
                        "exit": str(t.exit_ts)[11:19], "exit_price": float(t.exit_price),
                        "reason": t.exit_reason, "net": round(float(t.net_pnl), 2)}
                       for t in tr.itertuples()] if len(tr) else [],
            "gaps": len(prov.missing),
            "cpr_width": round(prov.prev_cpr_width(d), 2) if prov.prev_cpr_width(d) is not None else None,
            "cpr_skipped": any(e["event"] == "skipped_cpr" for e in r["events"]),
            "computed": dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M"),
        }
        out[d] = row
        log(f"{d}: backtest {row['trades']} trade(s) net {row['net']:,.0f} (breakeven={be})")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dict(sorted(out.items())), indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", nargs="*")
    a = ap.parse_args()
    today = dt.datetime.now(IST).date()
    dates = a.dates or ([today.isoformat()] if today.weekday() < 5 else [])
    if not dates:
        log("weekend -- nothing to do")
        return
    ensure_index(dates)
    sync_expiries()
    backtest(dates)


if __name__ == "__main__":
    main()
