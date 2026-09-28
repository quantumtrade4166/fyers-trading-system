"""
backtest/compare_live_vs_history.py — does Delta's history match what we saw live?
===================================================================================

For every COMPLETED live paper cycle of the BTC VWAP strategy, runs the same
settlement day three ways and puts them side by side with the live result:

  HIST-own     Delta history (validation pull), backtest picks its own strikes -
               exactly how the 2026 backtest was run
  HIST-forced  Delta history, forced to the live strikes and live first candle -
               isolates the DATA from the strike choice
  ARCH-forced  our own chain archive (real 1-min bid/ask), same forced strikes

and then checks the data directly for the live legs:
  - mark: Delta history 1-min mark vs our archived mark at the same minute
  - candles: history-built 5-min candles vs the candles the live engine built
    (close, VWAP, and whether each candle gave the same entry/exit signal)

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\backtest\\compare_live_vs_history.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("BTC_HIST_DIR", str(ROOT.parents[1] / "Bitcoin options data" / "validation"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import datetime as dt

import pandas as pd

from vwap_history import run_cycle, load_day
from vwap_archive_day import load_archive_day

RES = ROOT / "data" / "vwap_results"


def settle_day(cycle_key: str, version: str) -> str:
    start = dt.datetime.fromisoformat(cycle_key)
    return (start.date() + dt.timedelta(days=1 if version == "full_expiry" else 0)).isoformat()


def net(r):
    return None if r.get("skip") else round(r["net"], 2)


def signal(c):
    s = "S" if (c["c"] < c["o"] and c["c"] < c["vwap"]) else ""
    return s + ("X" if c["c"] > c["vwap"] else "")


def main():
    cycles = [json.loads(l) for l in (RES / "cycles.jsonl").read_text(encoding="utf-8").splitlines()
              if l.strip().startswith("{")]
    hist_cache, arch_cache = {}, {}
    table, candle_rows, mark_rows = [], [], []

    for cy in cycles:
        v, key = cy["version"], cy["cycle"]
        day = settle_day(key, v)
        chart = RES / "charts" / f"{v}_{key.replace(':', '')}.json"
        if not chart.exists():
            continue
        live = json.loads(chart.read_text(encoding="utf-8"))
        live_c = live.get("candles") or []
        if not live_c:
            continue
        if day not in hist_cache:
            hist_cache[day] = load_day(day)
        if day not in arch_cache:
            try:
                arch_cache[day] = load_archive_day(day)
            except SystemExit:
                arch_cache[day] = None
        H, A = hist_cache[day], arch_cache[day]
        if H is None:
            print(f"  {v} {key}: no history file for {day}")
            continue

        force = {"ce": cy["ce"], "pe": cy["pe"], "first_candle": live_c[0]["t"],
                 "combined": cy.get("combined_at_select")}
        own = run_cycle(v, day, H)
        hf = run_cycle(v, day, H, force=force)
        af = (run_cycle(v, day, A, force=force, real_book=True)
              if A is not None else {"skip": "no archive"})

        label = f"{v[:4]} {key[5:16].replace('T', ' ')}"
        table.append({
            "cycle": label, "settle": day[5:],
            "clean": not cy.get("late_start") and key[:10] != "2026-09-15",
            "live_strikes": f"{cy['ce']:.0f}/{cy['pe']:.0f}",
            "own_strikes": "-" if own.get("skip") else f"{own['ce']:.0f}/{own['pe']:.0f}",
            "live_n": cy["trades"], "live": round(cy["realized"], 2),
            "own_n": 0 if own.get("skip") else len(own["trades"]), "own": net(own),
            "hf_n": 0 if hf.get("skip") else len(hf["trades"]), "hf": net(hf),
            "af_n": 0 if af.get("skip") else len(af["trades"]), "af": net(af),
        })

        # candles: history-built vs live-built, same strikes, same buckets
        if not hf.get("skip"):
            hc = {c["t"]: c for c in hf["candle_list"]}
            dc = dv = n = same = 0
            for c in live_c:
                h = hc.get(c["t"])
                if not h or c.get("vwap") is None or h.get("vwap") is None:
                    continue
                n += 1
                dc += abs(h["c"] - c["c"]) / max(1e-9, c["c"])
                dv += abs(h["vwap"] - c["vwap"]) / max(1e-9, c["vwap"])
                same += signal(c) == signal(h)
            candle_rows.append({"cycle": label, "candles": n,
                                "close_err_%": round(100 * dc / n, 2) if n else None,
                                "vwap_err_%": round(100 * dv / n, 2) if n else None,
                                "same_signal_%": round(100 * same / n, 1) if n else None})

        # marks: Delta history vs our archive, same legs, same minutes
        if A is not None:
            for strike, typ in ((cy["ce"], "CE"), (cy["pe"], "PE")):
                h = H[(H["strike"] == strike) & (H["opt_type"] == typ)][["time_ist", "mark"]]
                a = A[(A["strike"] == strike) & (A["opt_type"] == typ)][["time_ist", "mark", "bid", "ask"]]
                m = h.merge(a, on="time_ist", suffixes=("_hist", "_arch"))
                if m.empty:
                    continue
                d = (m["mark_hist"] - m["mark_arch"]).abs()
                inside = ((m["mark_hist"] >= m["bid"]) & (m["mark_hist"] <= m["ask"])).mean()
                mark_rows.append({"cycle": label, "leg": f"{strike:.0f}{typ}",
                                  "minutes": len(m),
                                  "mean_abs_$": round(d.mean(), 2),
                                  "mean_rel_%": round(100 * (d / m["mark_arch"].clip(lower=0.5)).mean(), 2),
                                  "max_abs_$": round(d.max(), 2),
                                  "hist_inside_bid_ask_%": round(100 * inside, 1)})

    pd.set_option("display.width", 220)
    print("\n=== P&L per cycle: LIVE vs backtests (USD, net of fees) ===")
    t = pd.DataFrame(table)
    print(t.to_string(index=False))
    clean = t[t["clean"]]
    print(f"\n  clean cycles ({len(clean)}): live ${clean['live'].sum():+.2f} | "
          f"HIST-own ${clean['own'].fillna(0).sum():+.2f} | "
          f"HIST-forced ${clean['hf'].fillna(0).sum():+.2f} | "
          f"ARCH-forced ${clean['af'].fillna(0).sum():+.2f}")
    print("\n=== Candles: Delta history vs what the live engine built (same strikes) ===")
    print(pd.DataFrame(candle_rows).to_string(index=False))
    print("\n=== Marks: Delta history vs our live archive, per traded leg ===")
    print(pd.DataFrame(mark_rows).to_string(index=False))


if __name__ == "__main__":
    main()
