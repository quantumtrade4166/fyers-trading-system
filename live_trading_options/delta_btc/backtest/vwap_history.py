"""
backtest/vwap_history.py — replay the BTC VWAP strangle over the downloaded history.
====================================================================================

Runs the LIVE rule code (vwap/strategy.py: Session, select_pair, CandleBuilder,
Trigger) over `Bitcoin options data/`, so what is measured here is the same
strategy the paper engine runs, not a re-implementation of it.

Both versions, exactly as configured:
    ist_day       D 09:30 -> D 17:10 IST, contract settling D, combined <= 100
    full_expiry   D-1 17:35 -> D 17:10 IST, same contract, farthest equidistant
                  pair when nothing is <= 100 (the live fallback)

WHAT THE DATA GIVES, AND WHAT HAD TO BE MODELLED
  - 1-minute MARK bars per contract (open/high/low/close) + 1-minute spot: the
    combined-premium candles and the VWAP are built from these exactly as live —
    each minute's synchronized open and close are the only wick points (the NIFTY
    smoothed-wick method).
  - traded volume per minute per leg: real, from the same bars. VWAP weights it.
  - NO bid/ask and NO depth in the history. Fills are modelled around the mark
    with the SAME assumption as the delta-neutral BTC backtest:
        spread = max($2, 5% of mark),  sell at mark - half,  buy at mark + half
    which is about the 75th percentile of the spreads measured on our own live
    chain archive — slightly pessimistic. Delta's fee is charged per side.
  - Because there is no depth, the live 5%-of-trigger bad-fill guard is applied
    against this modelled fill; in the real book it refuses more often than this
    (see 2026-09-16 00:10), so live entries will sometimes be later than here.

Early Delta India history has nonsense marks on some strikes; a day whose
neighbouring-strike marks break no-arbitrage by >$20 more than 0.2% of the time is
skipped, same test as the delta-neutral backtest.

    .venv\\Scripts\\python.exe live_trading_options\\delta_btc\\backtest\\vwap_history.py --year 2026
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import json
import argparse
import datetime as dt
from multiprocessing import Pool

import pandas as pd

from vwap.strategy import Session, CandleBuilder, Trigger, select_pair, bucket_start

import os
# BTC_HIST_DIR points the run at another folder in the same format (e.g. the
# validation pull of recent days). An env var, not an argument, because the worker
# processes re-import this module and would not see a value set in main().
DATA = Path(os.environ.get("BTC_HIST_DIR") or (ROOT.parents[1] / "Bitcoin options data"))
PARAMS = json.loads((ROOT / "config" / "vwap_parameters.json").read_text(encoding="utf-8"))

# ── the spread model ─────────────────────────────────────────────────────
# MEASURED, not assumed: config/spread_model.json is written by
# backtest/calibrate_spread.py from 2.7M real bid/ask quotes in our chain archive,
# as the $ spread per mark bucket. BTC_SPREAD picks the statistic: "median" (the
# typical market, the base case) or "p75" (a wide market — the stress case), or
# "legacy" for the old max($2, 5% of mark) so earlier runs can be reproduced.
SPREAD_MODE = os.environ.get("BTC_SPREAD", "median")
# BTC_GUARD=off skips the 5%-of-trigger bad-fill guard. With a MODELLED spread the
# guard compares one average spread against the trigger every time, while live
# retries every 2 s and fills the moment the real book tightens — so on cheap legs
# the backtest guard refuses fills that live actually got (18-Sep ist_day: live 2
# trades, guarded backtest 0). Which setting tracks live better is measured by
# backtest/compare_live_vs_history.py.
GUARD = os.environ.get("BTC_GUARD", "on") != "off"
_SM_F = ROOT / "config" / "spread_model.json"
_SM = json.loads(_SM_F.read_text(encoding="utf-8")) if _SM_F.exists() else None


def spread_for(mark: float) -> float:
    """Full bid-ask spread in $ for a leg marked at `mark`."""
    if SPREAD_MODE == "legacy" or _SM is None:
        return max(2.0, 0.05 * mark)
    col = _SM["p75"] if SPREAD_MODE == "p75" else _SM["spread"]
    i = 0
    for k, edge in enumerate(_SM["edges"]):
        if mark >= edge:
            i = k
    return float(col[i])
OUT = ROOT / "data" / "vwap_results" / "backtest"
VERSIONS = ("ist_day", "full_expiry")


# ── the day's book ───────────────────────────────────────────────────────
def load_day(day: str):
    raw_f, sp_f = DATA / "raw" / f"{day}.parquet", DATA / "spot" / f"{day}.parquet"
    if not raw_f.exists() or not sp_f.exists():
        return None
    raw = pd.read_parquet(raw_f)
    if raw.empty:
        return None
    sp = pd.read_parquet(sp_f)[["time_ist", "close"]].rename(columns={"close": "spot"})
    df = raw.dropna(subset=["mark"]).merge(sp, on="time_ist", how="inner")
    if df.empty:
        return None
    half = df["mark"].map(spread_for) / 2
    df["bid"] = (df["mark"] - half).clip(lower=0.1)
    df["ask"] = df["mark"] + half
    df["mark_open"] = df["mark_open"].fillna(df["mark"])
    df["volume"] = df["volume"].fillna(0.0)
    return df


def bad_marks(df) -> float:
    """Share of neighbouring-strike pairs breaking no-arbitrage by > $20."""
    s = df[df["time_ist"].dt.minute % 15 == 0]
    bad = tot = 0
    for (_, ot), g in s.groupby([s["time_ist"], "opt_type"]):
        m = g.sort_values("strike")["mark"].values
        d = (m[1:] - m[:-1]) if ot == "PE" else (m[:-1] - m[1:])
        bad += int((d < -20).sum())
        tot += len(d)
    return bad / tot if tot else 1.0


def fee_for(price, contracts, spot, cv=0.001):
    f = PARAMS.get("fees") or {}
    qty = contracts * cv
    prem = float(f.get("premium_cap_rate", 0.035)) * price * qty
    return round(min(float(f.get("taker_rate_notional", 0.0001)) * spot * qty, prem), 6)


# ── one cycle ────────────────────────────────────────────────────────────
def run_cycle(version: str, day: str, df, fallback: bool = None,
              real_book: bool = False, force: dict = None) -> dict:
    """`fallback` overrides the version's fallback_last_pair. `real_book` fills at
    the bid/ask columns of the minute (the chain archive has real ones) instead of
    modelling a spread around the triggering price."""
    cfg = PARAMS["versions"][version]
    sess = Session(version, cfg)
    minutes = int(PARAMS.get("candle_minutes", 5))
    offset = float(PARAMS.get("trigger_offset", 1.0))
    max_slip = float(PARAMS.get("max_entry_slippage_pct", 5)) / 100.0
    contracts = sess.contracts
    D = dt.date.fromisoformat(day)

    if version == "ist_day":
        start = dt.datetime.combine(D, sess.start)
    else:
        start = dt.datetime.combine(D - dt.timedelta(days=1), sess.start)
    end = dt.datetime.combine(D, sess.square_off)
    cutoff = dt.datetime.combine(D, sess.cutoff)

    win = df[(df["time_ist"] >= start) & (df["time_ist"] <= end)]
    if win.empty:
        return {"day": day, "version": version, "skip": "no rows in window"}

    # strike selection: at the first minute where a qualifying pair exists.
    # `force` = {"ce", "pe", "first_candle"} replays the strikes and start the LIVE
    # engine actually used, so a comparison measures the data, not the selection.
    pick = sel_at = None
    if force:
        fc = pd.Timestamp(force["first_candle"])
        pick = {"ce": float(force["ce"]), "pe": float(force["pe"]),
                "combined": float(force.get("combined") or 0), "over_threshold": False}
        sel_at = fc.to_pydatetime()
    for minute, g in ([] if force else win.groupby("time_ist", sort=True)):
        marks = {(float(r.strike), r.opt_type): float(r.mark) for r in g.itertuples()}
        strikes = sorted({float(r.strike) for r in g.itertuples()})
        pick = select_pair(marks, strikes, float(g["spot"].iloc[0]), sess.threshold,
                           fallback_last=sess.fallback_last if fallback is None else fallback)
        if pick:
            sel_at = minute
            break
    if not pick:
        return {"day": day, "version": version, "skip": "no pair at/under threshold"}

    legs = win[((win["strike"] == pick["ce"]) & (win["opt_type"] == "CE")) |
               ((win["strike"] == pick["pe"]) & (win["opt_type"] == "PE"))]
    piv = legs.pivot_table(index="time_ist", columns="opt_type",
                           values=["mark_open", "mark", "bid", "ask", "volume"])
    piv = piv.dropna(subset=[("mark", "CE"), ("mark", "PE")]).sort_index()
    if len(piv) < 30:
        return {"day": day, "version": version, "skip": "thin leg data"}
    spot_by_min = win.groupby("time_ist")["spot"].first()

    first = bucket_start(sel_at, minutes)
    if sel_at > first:                       # never a partial first candle
        first = first + dt.timedelta(minutes=minutes)
    builder = CandleBuilder(first, minutes)

    state = {"pos": None, "minute": None, "trades": []}

    def px(minute, side, base=None):
        """Fill for the pair, priced off the price that actually triggered.

        `base` is the combined value the strategy acted on (the trigger tick, or a
        candle's close). It is split between the legs in proportion to their marks
        that minute and each leg crosses the modelled spread. Pricing off the
        minute's closing mark instead would fill an entry at a price the strategy
        never saw — sometimes better than the trigger, which is not a fill."""
        row = piv.loc[minute]
        if real_book:
            col = "bid" if side == "SELL" else "ask"
            c, p = float(row[(col, "CE")]), float(row[(col, "PE")])
            return round(c + p, 4), (c, p)
        cm, pm = float(row[("mark", "CE")]), float(row[("mark", "PE")])
        if base is None or cm + pm <= 0:
            c, p = cm, pm
        else:
            share = cm / (cm + pm)
            c, p = base * share, base * (1 - share)
        out = []
        for m in (c, p):
            half = spread_for(m) / 2
            out.append(round(max(0.1, m - half) if side == "SELL" else m + half, 4))
        return round(out[0] + out[1], 4), (out[0], out[1])

    def on_entry(trigger, n, reason):
        comb, (c, p) = px(state["minute"], "SELL", state.get("val"))
        if GUARD and comb < trigger * (1 - max_slip):
            return "retry"                   # same guard as live: sell nothing, stay armed
        spot = float(spot_by_min.loc[state["minute"]])
        fees = fee_for(c, contracts, spot) + fee_for(p, contracts, spot)
        state["pos"] = {"n": n, "trigger": trigger, "entry_time": str(state["minute"]),
                        "ce_sell": c, "pe_sell": p, "entry_combined": comb,
                        "entry_slippage": round(comb - trigger, 4),
                        "entry_fees": round(fees, 6)}
        return True

    def on_exit(n, reason):
        comb, (c, p) = px(state["minute"], "BUY", state.get("exit_base"))
        spot = float(spot_by_min.loc[state["minute"]])
        pos = state["pos"]
        fees = pos["entry_fees"] + fee_for(c, contracts, spot) + fee_for(p, contracts, spot)
        pts = round(pos["entry_combined"] - comb, 4)
        gross = round(pts * contracts * 0.001, 4)
        state["trades"].append(dict(pos, exit_time=str(state["minute"]), exit_reason=reason,
                                    ce_exit=c, pe_exit=p, exit_combined=comb, points=pts,
                                    gross_usd=gross, fees=round(fees, 4),
                                    net_usd=round(gross - fees, 4)))
        state["pos"] = None
        return True

    tr = Trigger(on_entry, on_exit, max_entries=sess.max_entries, cutoff=cutoff,
                 offset=offset, minutes=minutes)

    vol_bucket = {}
    for minute in piv.index:
        if minute < first:
            continue
        row = piv.loc[minute]
        o = float(row[("mark_open", "CE")]) + float(row[("mark_open", "PE")])
        c = float(row[("mark", "CE")]) + float(row[("mark", "PE")])
        b = bucket_start(minute, minutes)
        vol_bucket[b] = vol_bucket.get(b, 0.0) + float(row[("volume", "CE")]) + float(row[("volume", "PE")])

        # the minute's open, then its close — the only two wick points, as live
        state["minute"] = minute
        for val in (o, c):
            closed = builder.add(minute, val)
            if closed:
                fin = builder.finalize(closed, vol_bucket.pop(closed["start"], 0.0))
                tr.in_pos = state["pos"] is not None
                state["exit_base"] = fin["close"]      # an exit fills at the close
                tr.on_candle_close(fin)
            state["val"] = val                          # an entry fills at the trigger tick
            tr.on_tick(val, minute)

    # square-off anything still open, at the last minute of the window
    if state["pos"] is not None:
        state["minute"] = piv.index[-1]
        last = piv.loc[state["minute"]]
        state["exit_base"] = float(last[("mark", "CE")]) + float(last[("mark", "PE")])
        on_exit(tr.entries, f"square-off {sess.square_off:%H:%M}")

    net = round(sum(t["net_usd"] for t in state["trades"]), 4)
    candles_out = [{"t": c["start"].strftime("%Y-%m-%d %H:%M"), "o": c["open"], "h": c["high"],
                    "l": c["low"], "c": c["close"], "v": c.get("volume"), "vwap": c.get("vwap")}
                   for c in builder.closed]
    return {"day": day, "version": version, "ce": pick["ce"], "pe": pick["pe"],
            "candle_list": candles_out,
            "over_threshold": pick["over_threshold"], "selected_at": str(sel_at),
            "combined_at_select": pick["combined"], "candles": len(builder.closed),
            "trades": state["trades"], "net": net,
            "fees": round(sum(t["fees"] for t in state["trades"]), 4)}


def run_day(day: str):
    try:
        df = load_day(day)
        if df is None or df["time_ist"].nunique() < 600:
            return day, {"skip": "thin day"}
        if bad_marks(df) > 0.002:
            return day, {"skip": "broken marks"}
        out = {v: run_cycle(v, day, df) for v in VERSIONS}
        for r in out.values():
            r.pop("candle_list", None)       # the year file keeps trades, not candles
        return day, out
    except Exception as e:
        return day, {"skip": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="2026")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--days", type=int, default=0, help="limit, for a quick check")
    ap.add_argument("--tag", default="", help="suffix for the results file")
    ap.add_argument("--only", nargs="*", help="exact settlement days to run")
    a = ap.parse_args()

    days = sorted(p.stem for p in (DATA / "raw").glob(f"{a.year}-*.parquet"))
    if a.days:
        days = days[:a.days]
    if a.only:
        days = [d for d in days if d in set(a.only)]
    OUT.mkdir(parents=True, exist_ok=True)
    res_f = OUT / f"vwap_backtest_{a.year}{a.tag}.json"
    done = json.loads(res_f.read_text(encoding="utf-8")) if res_f.exists() else {}
    todo = [d for d in days if d not in done]
    print(f"  {len(days)} days, {len(todo)} to run")

    with Pool(a.workers) as pool:
        for i, (day, out) in enumerate(pool.imap_unordered(run_day, todo), 1):
            done[day] = out
            if i % 10 == 0 or i == len(todo):
                res_f.write_text(json.dumps(done, default=str), encoding="utf-8")
            if out.get("skip"):
                s = f"skipped ({out['skip']})"
            else:
                s = "  ".join(
                    f"{v[:4]} " + (f"skip" if out[v].get("skip") else
                                   f"{len(out[v]['trades'])}t {out[v]['net']:+8.2f}")
                    for v in VERSIONS)
            print(f"  [{i}/{len(todo)}] {day}  {s}", flush=True)
    res_f.write_text(json.dumps(done, default=str), encoding="utf-8")
    print(f"\n  written {res_f}")


if __name__ == "__main__":
    main()
