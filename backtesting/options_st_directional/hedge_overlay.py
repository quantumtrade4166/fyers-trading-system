"""Hedged version of the strategy: add a bought far-OTM wing to every trade.

The hedge never changes a signal (entries, option-ST exits, stop-to-cost and the
15:15 square-off all key off the SOLD leg only), so the sold-leg trades of an
existing run are reused unchanged and the wing is overlaid on them:

  * at the sold leg's entry minute, walk strikes further OTM (same type, same
    expiry, 50-pt steps) and BUY the nearest one whose price <= HEDGE_PCT x the
    sold leg's entry price
  * SELL the wing back at the sold leg's exit time
  * wing price at a time = open of that 1-minute bar (close of the minute for a
    stop-to-cost exit, which happens inside the minute); if that minute did not
    trade, the last close before it (<= 15 min old), else the next bar's open
  * costs: statutory_costs on the wing round trip + slippage per side

Results are cached per trade in results/HEDGE_{pct}/hedge_legs.csv so an
interrupted run resumes where it stopped.

    .venv\\Scripts\\python.exe -m backtesting.options_st_directional.hedge_overlay --pct 0.10
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
from pathlib import Path

import pandas as pd

from backtesting.options_pivot_intraday.costs import statutory_costs
from backtesting.options_st_directional.config import Config
from backtesting.options_st_directional.data import BreezeProvider

RES = Path(__file__).resolve().parent / "results"
RUNS = [f"Y{y}_10LOTS_BE25" for y in (2024, 2025, 2026)]
MAX_STEPS = 60                     # up to 3,000 points OTM


def price_at(prov, day, contract, ts, use_close=False):
    df = prov._bars(day, contract)
    if df.empty:
        return None
    ts = pd.Timestamp(ts).floor("min")
    row = df[df["datetime"] == ts]
    if not row.empty:
        return float(row.iloc[0]["close" if use_close else "open"])
    before = df[(df["datetime"] < ts) & (df["datetime"] >= ts - pd.Timedelta(minutes=15))]
    if not before.empty:
        return float(before.iloc[-1]["close"])
    after = df[df["datetime"] > ts]
    if not after.empty and after.iloc[0]["datetime"] - ts <= pd.Timedelta(minutes=15):
        return float(after.iloc[0]["open"])
    return None


_last_k = {"CE": 10, "PE": 14}       # last hit distance (in 50-pt steps) per type


def pick_wing(prov, tr, pct):
    """Nearest OTM strike priced <= pct x sold premium at entry. Returns (strike, price, probes).

    Premium falls as strikes move OTM, so the answer is the smallest k with
    price(k) <= threshold. The search starts at the previous trade's distance and
    walks inward or outward from there (a few probes instead of a scan)."""
    day, opt = tr["date"], tr["opt_type"]
    exp, k0 = pd.Timestamp(tr["expiry"]), int(tr["strike"])
    thr = pct * float(tr["entry_price"])
    sign = 1 if opt == "CE" else -1
    cache, probes = {}, 0

    def ok(k):
        nonlocal probes
        if k not in cache:
            probes += 1
            cache[k] = price_at(prov, day, (exp, k0 + sign * 50 * k, opt), tr["entry_ts"])
        return cache[k] is not None and cache[k] <= thr

    k = max(1, _last_k[opt])
    if ok(k):
        while k > 1 and ok(k - 1):                 # walk inward while still cheap enough
            k -= 1
    else:
        while k < MAX_STEPS and not ok(k + 1):     # walk outward until cheap enough
            k += 1
        k += 1
        if k > MAX_STEPS or not ok(k):
            return None, None, probes
    _last_k[opt] = k
    return k0 + sign * 50 * k, cache[k], probes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pct", type=float, default=0.10)
    ap.add_argument("--slippage", type=float, default=0.5)
    a = ap.parse_args()

    out = RES / f"HEDGE_{int(a.pct * 100)}PCT"
    out.mkdir(exist_ok=True)
    legs_f = out / "hedge_legs.csv"

    trades = pd.concat([pd.read_csv(RES / r / "trades.csv") for r in RUNS], ignore_index=True)
    trades = trades.drop_duplicates("trade_id").sort_values("entry_ts").reset_index(drop=True)
    done = pd.read_csv(legs_f) if legs_f.exists() else pd.DataFrame(columns=["trade_id"])
    todo = trades[~trades["trade_id"].isin(set(done["trade_id"]))]
    print(f"{len(trades)} trades, {len(done)} already hedged, {len(todo)} to do", flush=True)

    prov = BreezeProvider(Config(start="2024-01-01", end="2026-12-31"), verbose=False)
    rows = []
    for i, tr in enumerate(todo.to_dict("records"), 1):
        strike, hb, probes = pick_wing(prov, tr, a.pct)
        rec = {"trade_id": tr["trade_id"], "date": tr["date"], "opt_type": tr["opt_type"],
               "sold_strike": tr["strike"], "expiry": tr["expiry"],
               "sold_entry": tr["entry_price"], "threshold": round(a.pct * tr["entry_price"], 2),
               "hedge_strike": strike, "hedge_buy": hb, "hedge_sell": None,
               "hedge_otm_pts": None if strike is None else abs(strike - tr["strike"]),
               "probes": probes, "note": ""}
        if strike is None:
            rec["note"] = "no strike <= threshold found (unhedged)"
        else:
            c = (pd.Timestamp(tr["expiry"]), strike, tr["opt_type"])
            be = tr["exit_reason"] == "breakeven_stop"
            hs = price_at(prov, tr["date"], c, tr["exit_ts"], use_close=be)
            if hs is None:
                hs = 0.05
                rec["note"] = "no exit price; wing assumed worthless (0.05)"
            rec["hedge_sell"] = hs
        rows.append(rec)
        if i % 10 == 0 or i == len(todo):
            pd.concat([done, pd.DataFrame(rows)], ignore_index=True).to_csv(legs_f, index=False)
            print(f"  {i}/{len(todo)}  {tr['date']}  {tr['strike']}{tr['opt_type']} "
                  f"-> wing {strike} @ {hb}", flush=True)
    if rows:
        done = pd.concat([done, pd.DataFrame(rows)], ignore_index=True)
        done.to_csv(legs_f, index=False)
    summarize(trades, done, a, out)


def summarize(trades, legs, a, out):
    from backtesting.options_st_directional.config import lot_size
    t = trades.merge(legs[["trade_id", "hedge_strike", "hedge_buy", "hedge_sell", "note"]],
                     on="trade_id", how="left")
    t["qty"] = t["expiry"].map(lot_size) * 10
    t["sold_net"] = [(r.entry_price - r.exit_price) * r.qty
                     - statutory_costs(r.entry_price, r.exit_price, r.qty, r.date)["total"]
                     - a.slippage * 2 * r.qty for r in t.itertuples()]
    hed = []
    for r in t.itertuples():
        if pd.isna(r.hedge_buy):
            hed.append(0.0)
            continue
        hed.append((r.hedge_sell - r.hedge_buy) * r.qty
                   - statutory_costs(r.hedge_sell, r.hedge_buy, r.qty, r.date)["total"]
                   - a.slippage * 2 * r.qty)
    t["hedge_net"] = hed
    t["hedged_net"] = t["sold_net"] + t["hedge_net"]
    t.to_csv(out / f"trades_hedged_slip{a.slippage}.csv", index=False)
    print(f"\nsaved {out / f'trades_hedged_slip{a.slippage}.csv'}")


if __name__ == "__main__":
    main()
