import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Universe runner — run any book strategy over a whole market (many symbols)
and report it the same way every time.

A strategy is a plain function   fn(df, cfg) -> signals frame
(see engine/harness.py). df already carries the VPA features from
engine/vpa.py. The runner adds the session rules itself (no entries after
the cut-off, forced square-off), so strategies stay market-agnostic.

Spec = (strategy_id, "module.path", "function_name", cfg_id, cfg_dict)

Results: every trade, tagged with strategy / config / symbol, plus R
(P&L in units of the fixed per-trade risk). Summary splits in-sample and
out-of-sample at the market's oos_start date.
"""

import importlib
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from backtesting.engine import harness, vpa
from backtesting.engine.markets import MARKETS

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"


# ── Data: Indian stocks, 5-minute ────────────────────────────────────────────

def list_in_stocks() -> list[str]:
    """Every NSE_*_EQ folder that holds 5-minute data (folder scan, not the
    manifest — the manifest was stale: 61 listed vs 182 on disk)."""
    return sorted(p.name for p in DATA.glob("NSE_*_EQ")
                  if any(p.glob("*/ohlcv_5min.parquet")))


def load_in_stock(folder: str) -> pd.DataFrame:
    parts = [pd.read_parquet(f) for f in sorted((DATA / folder).glob("*/ohlcv_5min.parquet"))]
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = (df.drop_duplicates("datetime").set_index("datetime").sort_index()
            [["open", "high", "low", "close", "volume"]])
    t = df.index.time
    df = df[(t >= pd.Timestamp("09:15").time()) & (t <= pd.Timestamp("15:25").time())]
    return df[(df["volume"] > 0) & (df["high"] >= df["low"])]


# ── Data: Nifty 500, daily ───────────────────────────────────────────────────
N500 = ROOT / "Nifty 500 Daily Fyers"


def list_nifty500() -> list[str]:
    return sorted(p.stem for p in N500.glob("*.parquet"))


def load_nifty500(sym: str) -> pd.DataFrame:
    df = pd.read_parquet(N500 / f"{sym}.parquet")
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated()].sort_index()[["open", "high", "low", "close", "volume"]]
    return df[(df["volume"] > 0) & (df["high"] >= df["low"]) & (df["low"] > 0)]


from backtesting.engine import bhav  # noqa: E402

LOADERS = {"in_stocks_5m": (list_in_stocks, load_in_stock),
           "nifty500_1d": (list_nifty500, load_nifty500),
           "bhav_1d": (bhav.list_symbols, bhav.load)}   # survivorship-free NSE


# ── Session rules ────────────────────────────────────────────────────────────

def session_masks(df: pd.DataFrame, mk: dict) -> tuple[np.ndarray, np.ndarray]:
    if not mk["intraday"]:                       # daily: no session rules
        return np.ones(len(df), bool), np.zeros(len(df), bool)
    t = df.index.time
    day = df.index.normalize()
    last_of_day = np.r_[day[1:] != day[:-1], True]
    entry_ok = t <= pd.Timestamp(mk["last_entry"]).time()
    force = (t >= pd.Timestamp(mk["squareoff"]).time()) | last_of_day
    return entry_ok, force


# ── Long + short from one long-only strategy ─────────────────────────────────

def combine_sides(fn, feat: pd.DataFrame, feat_m: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """
    Strategies are written LONG-only. The short side is the same function run
    on the price-mirrored features (vpa.mirror), with its stop flipped back.
    cfg["sides"]: "both" (default) | "long" | "short".
    """
    sides = cfg.get("sides", "both")
    sig = harness.blank_signals(feat)
    if sides in ("both", "long"):
        L = fn(feat, cfg)
        sig["entry"] += (L["entry"] > 0).astype(int)
        sig["exit_long"] = L["exit_long"].fillna(False).astype(bool)
        sig["stop_long"] = L["stop_long"]
    if sides in ("both", "short"):
        S = fn(feat_m, cfg)
        sig["entry"] -= (S["entry"] > 0).astype(int)
        sig["exit_short"] = S["exit_long"].fillna(False).astype(bool)
        sig["stop_short"] = -S["stop_long"]
    return sig


# ── One symbol, many strategy configs ────────────────────────────────────────

def _run_symbol(folder: str, specs: list, market: str) -> list[dict]:
    mk = MARKETS[market]
    df = LOADERS[mk["loader"]][1](folder)
    if len(df) < (2000 if mk["intraday"] else 250):
        return []
    feat = vpa.add_vpa_features(df, intraday=mk["intraday"])
    feat_m = vpa.add_vpa_features(vpa.mirror(df), intraday=mk["intraday"])
    entry_ok, force = session_masks(feat, mk)
    if "tradable" in feat:          # point-in-time universe (e.g. bhavcopy top 500)
        entry_ok = entry_ok & feat["tradable"].to_numpy(bool)
    jump = (feat["open"] / feat["close"].shift(1)).to_numpy()
    bad = (jump < 0.6) | (jump > 1.67)          # likely unadjusted corporate action
    pos = {t: k for k, t in enumerate(feat.index)}
    hcfg = {k: mk[k] for k in ("capital", "risk_pct", "max_position_pct",
                               "entry_on", "cost_pct", "calendar")}
    hcfg["atr_col"] = "atr"
    risk = mk["capital"] * mk["risk_pct"]

    rows = []
    for sid, mod, fn_name, cid, cfg in specs:
        fn = getattr(importlib.import_module(mod), fn_name)
        sig = combine_sides(fn, feat, feat_m, cfg)
        sig["entry"] = np.where(entry_ok, sig["entry"].fillna(0), 0)
        sig["force_exit"] = force | sig.get("force_exit", False)
        for tr in harness.run(feat, sig, {**hcfg, **cfg.get("harness", {})}):
            d = tr.to_dict()
            d.update(strategy=sid, cfg_id=cid, symbol=folder,
                     R=d["pnl"] / risk, R_gross=d["gross_pnl"] / risk)
            a, b = pos[d["entry_time"]], pos[d["exit_time"]]
            d["data_jump"] = bool(bad[a:b + 1].any())
            if "adv20" in feat:
                d["adv20"] = float(feat["adv20"].iat[a])
            rows.append(d)
    return rows


def run_universe(specs: list, market: str = "in_stocks_5m",
                 symbols: list[str] = None, workers: int = None) -> pd.DataFrame:
    symbols = symbols or LOADERS[MARKETS[market]["loader"]][0]()
    workers = workers or max(1, (os.cpu_count() or 2) - 2)
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_run_symbol, s, specs, market): s for s in symbols}
        for k, f in enumerate(as_completed(futs), 1):
            rows.extend(f.result())
            if k % 20 == 0 or k == len(futs):
                print(f"  {k}/{len(futs)} symbols done", flush=True)
    return pd.DataFrame(rows)


# ── Reporting ────────────────────────────────────────────────────────────────

def _stats(t: pd.DataFrame, start, end) -> dict:
    if t.empty:
        return {"trades": 0}
    daily = t.groupby(t["exit_time"].dt.normalize())["R"].sum()
    days = pd.bdate_range(start, end)
    daily = daily.reindex(days.union(daily.index), fill_value=0.0)
    eq = daily.cumsum()
    wins, losses = t.loc[t["R"] > 0, "R"].sum(), -t.loc[t["R"] < 0, "R"].sum()
    return {
        "trades":       len(t),
        "win_pct":      round((t["R"] > 0).mean() * 100, 1),
        "pf":           round(wins / losses, 2) if losses > 0 else np.inf,
        "avgR_net":     round(t["R"].mean(), 3),
        "avgR_gross":   round(t["R_gross"].mean(), 3),
        "totalR_net":   round(t["R"].sum(), 1),
        "sharpe":       round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() > 0 else 0.0,
        "maxdd_R":      round((eq - eq.cummax()).min(), 1),
        "symbols":      t["symbol"].nunique(),
    }


def summarize(trades: pd.DataFrame, market: str = "in_stocks_5m") -> pd.DataFrame:
    mk = MARKETS[market]
    if trades.empty:
        return pd.DataFrame()
    trades = trades.copy()
    trades["exit_time"] = pd.to_datetime(trades["exit_time"])
    cut = pd.Timestamp(mk["oos_start"])
    d0, d1 = trades["exit_time"].min().normalize(), trades["exit_time"].max().normalize()
    out = []
    for (sid, cid), g in trades.groupby(["strategy", "cfg_id"]):
        isd, oos = g[g["exit_time"] < cut], g[g["exit_time"] >= cut]
        row = {"strategy": sid, "cfg_id": cid}
        row.update({f"IS_{k}": v for k, v in _stats(isd, d0, cut - pd.Timedelta(days=1)).items()})
        row.update({f"OOS_{k}": v for k, v in _stats(oos, cut, d1).items()})
        out.append(row)
    return pd.DataFrame(out)
