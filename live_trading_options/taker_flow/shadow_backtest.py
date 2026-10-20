import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Taker Flow L/S — SHADOW BACKTEST: the backtest, run forward on the paper period.

Purpose: paper vs backtest on the SAME days with the SAME $10k, so any gap is
execution (real Delta fills, whole contracts, entry timing) and nothing else.

Pure backtest data and code:
  prices   Binance USDT-M daily bars (UTC); entries/exits at the next day's OPEN
  signal   backtesting/crypto_volume/engine.py xs_weights — the exact function behind
           the published backtest — 14d taker-buy ratio, PIT top-100 by 30d quote
           volume (listing age from Binance onboardDate), inverse-vol, q=0.2
  schedule weights decided at the close of the day BEFORE each paper rebalance date
           (paper rebalances every 3 days at 00:05 UTC from its start date)
  Delta    tradable set = coins with Delta 24h turnover >= $50k, from the daily Delta
           snapshot the paper engine saves (nearest earlier snapshot)
  costs    the backtest's "Delta realistic" model per coin: 5.9 bps (0.05% + 18% GST)
           + spread/2 + spread/2 x min(order / top-of-book depth, 4)
  funding  Delta's own funding history (FUNDING:<symbol> candles, % per 8h, /8 per hour)

Output: data/state/shadow.json — daily points from the paper start, the backtest's
picks at every rebalance, cost/funding totals.

Run: .venv\\Scripts\\python.exe -m live_trading_options.taker_flow.shadow_backtest
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.crypto_volume import engine as E
from .flow_signal import BINANCE, DELTA, EXCLUDE, _get, map_binance_to_delta

BASE = Path(__file__).parent
CFG = BASE / "config" / "parameters.json"
STATE = BASE / "data" / "state"
TAKER_BPS = 5.0 * 1.18
HISTORY_DAYS = 90                  # warm-up before the paper start (ADV 30, vol 30, age 30)
CANDIDATES = 300                   # busiest Binance perps by 24h volume that get daily history


def _klines(symbol: str, start_ms: int) -> pd.DataFrame:
    # Binance charges kline requests by `limit` (<100: 1, <500: 2, <=1000: 5, >1000: 10).
    # Ask only for the days needed: 300 coins x 1500 bars (weight 10) blew the
    # 2400/min budget and got this IP a 429 on 19-Oct.
    days = int((time.time() * 1000 - start_ms) / 86_400_000) + 2
    rows = _get(f"{BINANCE}/fapi/v1/klines",
                {"symbol": symbol, "interval": "1d", "startTime": start_ms, "limit": min(days, 1000)})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "ct", "quote_volume",
                                     "trades", "taker_buy", "taker_buy_quote", "ignore"])
    df["datetime"] = pd.to_datetime(df["t"], unit="ms")
    df = df.set_index("datetime")[["open", "close", "volume", "quote_volume", "taker_buy", "ct"]].astype(float)
    forming = df["ct"] >= time.time() * 1000
    # the forming bar contributes ONLY its open (the exit price of the last completed bar)
    df.loc[forming, ["close", "volume", "quote_volume", "taker_buy"]] = np.nan
    return df.drop(columns="ct")


def _delta_snapshots(state: Path) -> dict[str, dict]:
    snaps = {}
    for f in sorted((state / "delta_snapshots").glob("*.json")):
        try:
            snaps[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
    return snaps


def _snapshot_for(snaps: dict, day: str) -> dict:
    keys = [k for k in snaps if k <= day]
    return snaps[keys[-1]] if keys else snaps[min(snaps)]


def build_panel(start: pd.Timestamp, log=print):
    """A daily E.Panel built from the Binance API (same fields the backtest uses)."""
    info = [s for s in _get(f"{BINANCE}/fapi/v1/exchangeInfo")["symbols"]
            if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"]
    vol24 = {t["symbol"]: float(t["quoteVolume"]) for t in _get(f"{BINANCE}/fapi/v1/ticker/24hr")}
    info = sorted(info, key=lambda s: vol24.get(s["symbol"], 0.0), reverse=True)[:CANDIDATES]
    onboard = {s["symbol"]: pd.to_datetime(s["onboardDate"], unit="ms") for s in info}
    start_ms = int((start - pd.Timedelta(days=HISTORY_DAYS)).timestamp() * 1000)
    dfs = {}
    for i, s in enumerate(info):
        df = _klines(s["symbol"], start_ms)
        if len(df):
            dfs[s["symbol"]] = df
        time.sleep(0.15)                                # ~400 calls/min, weight <= 800/min
    log(f"shadow: daily history for {len(dfs)} Binance perps")

    P = E.Panel.__new__(E.Panel)
    P.tf, P.universe, P.bpy, P.bpd, P.dfs = "1d", "pit", 365, 1, {}
    P.index = pd.DatetimeIndex(sorted(set().union(*[d.index for d in dfs.values()])))
    col = lambda c: pd.DataFrame({p: d[c] for p, d in dfs.items()}).reindex(P.index)
    P.open, P.close, P.volume = col("open"), col("close"), col("volume")
    P.taker_buy, qv = col("taker_buy"), col("quote_volume")
    P.funding = pd.DataFrame(0.0, index=P.index, columns=P.close.columns)
    P.ret = P.open.shift(-1) / P.open - 1
    age_ok = pd.DataFrame({p: P.index >= onboard[p] + pd.Timedelta(days=E.WARMUP_DAYS)
                           for p in P.close.columns}, index=P.index)
    P.active = age_ok & P.close.notna() & P.ret.notna()
    adv = qv.rolling(30, min_periods=15).mean().where(age_ok)
    P.member = adv.rank(axis=1, ascending=False) <= 100
    P.active &= P.member
    P.vol = P.close.pct_change(fill_method=None).rolling(30, min_periods=10).std()
    return P


def delta_funding(P: E.Panel, sym_map: dict, start: pd.Timestamp) -> pd.DataFrame:
    """Per-day funding fraction from Delta's FUNDING candles, for the mapped coins."""
    fund = P.funding.copy()
    t0, t1 = int(start.timestamp()), int(time.time())
    for b, d in sym_map.items():
        try:
            rows = _get(f"{DELTA}/v2/history/candles",
                        {"symbol": f"FUNDING:{d}", "resolution": "1h", "start": t0, "end": t1})["result"]
        except Exception:
            continue
        if not rows:
            continue
        s = pd.Series({pd.to_datetime(r["time"], unit="s"): float(r["close"]) for r in rows}) / 100 / 8
        fund[b] = s.resample("1D").sum().reindex(fund.index).fillna(0.0)
        time.sleep(0.1)
    return fund


def run(state: Path = STATE, log=print) -> dict:
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    ledger = json.loads((state / "ledger.json").read_text(encoding="utf-8"))
    paper_start = pd.Timestamp(ledger["started"]).tz_convert(None).normalize()
    anchor = paper_start - pd.Timedelta(days=1)          # signal = close of the day before rebalance
    snaps = _delta_snapshots(state)
    if not snaps:
        raise RuntimeError("no Delta snapshot saved yet (the engine writes one daily)")

    P = build_panel(paper_start, log)
    # Delta-tradable mask from the snapshot in force on each day
    sym_map = {}
    allowed = pd.DataFrame(False, index=P.index, columns=P.close.columns)
    for day in P.index:
        snap = _snapshot_for(snaps, day.strftime("%Y-%m-%d"))
        for b in P.close.columns:
            u = map_binance_to_delta(b, snap)
            if u and snap[u]["turnover_usd"] >= cfg["min_delta_turnover_usd"]:
                allowed.loc[day, b] = True
                sym_map[b] = snap[u]["symbol"]

    lb = cfg["lookback_days"]
    tr = (P.taker_buy.rolling(lb, min_periods=lb).sum() / P.volume.rolling(lb, min_periods=lb).sum())
    tr = tr.where(allowed)

    # xs_weights rebalances at index 0, k, 2k... -> start the window ON the anchor
    W = P.index[P.index >= anchor]
    sub = E.Panel.__new__(E.Panel)
    sub.__dict__.update({k: (v.loc[W] if isinstance(v, (pd.DataFrame, pd.Series)) else v)
                         for k, v in P.__dict__.items()})
    sub.index = W
    w = cfg["leverage_per_book"] * E.xs_weights(sub, tr.loc[W], cfg["rebalance_every_days"],
                                                 q=cfg["bucket_q"], min_coins=cfg["min_coins"])

    # cost per coin from the latest Delta snapshot (spread + depth), same formula as the backtest
    snap = snaps[max(snaps)]
    cost = {}
    dw = w.diff().abs()
    order_usd = (dw.where(dw > 1e-9).mean() * cfg["capital_usd"]).fillna(0)
    for b in w.columns:
        u = map_binance_to_delta(b, snap)
        if not u:
            cost[b] = 30.0
            continue
        s = snap[u]
        mid = (s["best_bid"] + s["best_ask"]) / 2
        spread = (s["best_ask"] - s["best_bid"]) / mid * 1e4 if mid > 0 and s["best_bid"] > 0 else 20.0
        depth = min(s["bid_size"], s["ask_size"]) * s["contract_value"] * mid
        imp = spread / 2 * min(order_usd[b] / depth, 4) if depth > 0 else spread / 2 * 4
        cost[b] = TAKER_BPS + spread / 2 + imp
    cost = pd.Series(cost)

    sub.funding = delta_funding(sub, {b: sym_map[b] for b in w.columns if (w[b] != 0).any() and b in sym_map},
                                paper_start)
    res = E.run(sub, w, cost)
    res = res.loc[paper_start:]
    res = res[res.index.isin(P.index[P.close.notna().any(axis=1)])]   # completed days only

    eq = cfg["capital_usd"] * (1 + res["net"]).cumprod()
    points = [{"date": (paper_start).strftime("%Y-%m-%d"), "equity": cfg["capital_usd"], "pnl": 0.0,
               "net": 0.0, "cost": 0.0, "funding": 0.0}]
    prev = cfg["capital_usd"]
    for d, r in res.iterrows():
        e = float(eq.loc[d])
        points.append({"date": (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),   # value at the END of day d
                       "equity": round(e, 2), "pnl": round(e - cfg["capital_usd"], 2),
                       "net": round(e - prev, 2), "cost": round(float(r["cost"]) * prev, 2),
                       "funding": round(float(r["funding"]) * prev, 2)})
        prev = e

    picks = []
    for d in W[::cfg["rebalance_every_days"]]:
        row = w.loc[d]
        picks.append({"signal_day": d.strftime("%Y-%m-%d"),
                      "rebalance": (d + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                      "longs": sorted([map_binance_to_delta(c, snap) or c for c in row[row > 0].index]),
                      "shorts": sorted([map_binance_to_delta(c, snap) or c for c in row[row < 0].index])})
    out = {"asof": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "start": paper_start.strftime("%Y-%m-%d"), "points": points, "picks": picks,
           "cost_bps_median": round(float(cost[[c for c in w.columns if (w[c] != 0).any()]].median()), 1)
           if (w != 0).any().any() else None,
           "method": "Binance daily bars, next-open fills, backtest cost model, Delta funding"}
    p = state / "shadow.json"
    p.with_suffix(".tmp").write_text(json.dumps(out), encoding="utf-8")
    p.with_suffix(".tmp").replace(p)
    log(f"shadow: {len(points) - 1} completed days, backtest P&L ${points[-1]['pnl']:+,.2f}")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=str(STATE))
    run(Path(ap.parse_args().state_dir))
