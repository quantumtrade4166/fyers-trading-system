import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
BTC screen — every Kaufman strategy x swing/daily timeframes x long/short modes
==================================================================================
Asset   : BTCUSDT (Binance 1m archive, 2017-08 -> now), costed as Delta
          Exchange India BTCUSD perp: 0.06%/side + 10% APR funding (longs pay)
Capital : $10,000, 1% risk per trade, max 50% of capital per position (no leverage)

Split — fit on the OLD data, judge on the NEW (the order we'd live it in):
  IS  = 2017-08 -> 2021-12-31  (2018 -84% crash, 2020-21 bull)
  OOS = 2022-01 -> now         (2022 -77% crash, 2023-25 bull) — never fitted

Both halves contain a full bear market, so a strategy cannot pass by being
long in a bull run.

Two directional modes per strategy:
  ls   long + short (perp)
  lo   long only — shorts become "stay flat"

⚠️ Selection-bias rule (learned the hard way on gold): this screen only
PRODUCES candidates. Anything chosen from this table must be chosen on the
IS columns and then read off the OOS columns — ranking by OOS and calling
the winners "validated" is exactly the mistake that invalidated the gold
portfolio. The report prints both rankings so the decay is visible.

Run: .venv\\Scripts\\python.exe backtesting\\book_strategies\\kaufman\\run_btc_screen.py
"""

import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data_btc import get_bars, DELTA_COST_PCT
from backtesting.book_strategies.kaufman.harness import run, metrics
from backtesting.book_strategies.kaufman.strategies_adaptive import REGISTRY as R_A
from backtesting.book_strategies.kaufman.strategies_channels import (
    REGISTRY as R_C, STRATEGY_CFG as CFG_C)
from backtesting.book_strategies.kaufman.strategies_bulk import (
    REGISTRY as R_B, STRATEGY_CFG as CFG_B)
from backtesting.book_strategies.kaufman.strategies_bulk2 import (
    REGISTRY as R_B2, STRATEGY_CFG as CFG_B2)

RESULTS_DIR = Path(__file__).parent / "results_btc"
RESULTS_DIR.mkdir(exist_ok=True)

CAPITAL = 10_000
SPLIT = "2022-01-01"
TIMEFRAMES = ["1h", "2h", "4h", "8h", "12h", "1D"]
MODES = ["ls", "lo"]
MIN_TRADES = {"1h": 40, "2h": 40, "4h": 30, "8h": 25, "12h": 20, "1D": 15}

REGISTRY = {**R_B, **R_B2, **R_A, **R_C}
PER_STRATEGY = {**CFG_B, **CFG_B2, **CFG_C, "S092_AdaptiveBrkout": {"trailing": True}}

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_pct": DELTA_COST_PCT, "funding_apr": 0.10, "calendar": "calendar",
}


def ratio(m: dict) -> float:
    dd = abs(m.get("max_drawdown_pct", 0))
    return m.get("cagr_pct", 0) / dd if dd > 0.01 else 0.0


def _bt(df: pd.DataFrame, name: str, mode: str) -> dict:
    cfg = {**BASE_CFG, **PER_STRATEGY.get(name, {})}
    sig = REGISTRY[name](df.copy(), cfg)
    if mode == "lo":
        sig = sig.copy()
        entry = sig["entry"].fillna(0)
        if cfg.get("mode") == "reversal":
            # A long-only reversal system: the short signal means "get out".
            sig["exit_long"] = sig["exit_long"].fillna(False).astype(bool) | (entry < 0)
            cfg = {**cfg, "mode": "flat"}
        sig["entry"] = entry.clip(lower=0)
    trades = run(df, sig, cfg)
    return metrics(trades, CAPITAL, calendar="calendar")


def _job(tf: str, name: str) -> list[dict]:
    d_is = get_bars(tf, end=SPLIT)
    d_oos = get_bars(tf, start=SPLIT)
    out = []
    for mode in MODES:
        try:
            mi = _bt(d_is, name, mode)
            mo = _bt(d_oos, name, mode)
        except Exception:
            continue
        if mi.get("total_trades", 0) < MIN_TRADES[tf] or mo.get("total_trades", 0) < MIN_TRADES[tf]:
            continue
        out.append({"strategy": name, "timeframe": tf, "mode": mode,
                    "is_ratio": ratio(mi), "oos_ratio": ratio(mo),
                    **{f"is_{k}": v for k, v in mi.items()},
                    **{f"oos_{k}": v for k, v in mo.items()}})
    return out


def buy_and_hold(df: pd.DataFrame) -> dict:
    c = df["close"].resample("1D").last().dropna()
    yrs = (c.index[-1] - c.index[0]).days / 365.25
    r = c.pct_change().dropna()
    dd = (c / c.cummax() - 1).min() * 100
    return {"cagr": ((c.iloc[-1] / c.iloc[0]) ** (1 / yrs) - 1) * 100,
            "dd": dd, "sharpe": r.mean() / r.std() * np.sqrt(365)}


def main() -> None:
    t0 = time.perf_counter()
    d1 = get_bars("1D")
    for tf in TIMEFRAMES:          # build caches once, before workers race for them
        get_bars(tf)

    print("=" * 118)
    print(f"  BTC SCREEN — {len(REGISTRY)} strategies x {len(TIMEFRAMES)} timeframes x {len(MODES)} modes")
    print(f"  data {d1.index.min().date()} -> {d1.index.max().date()} | split {SPLIT} | "
          f"${CAPITAL:,} 1% risk | cost {DELTA_COST_PCT*100:.2f}%/side + 10% APR funding")
    print("=" * 118)
    for label, part in (("IS ", d1[d1.index < SPLIT]), ("OOS", d1[d1.index >= SPLIT])):
        b = buy_and_hold(part)
        print(f"  Buy & hold BTC {label}: CAGR {b['cagr']:6.1f}%  MaxDD {b['dd']:6.1f}%  "
              f"Sharpe {b['sharpe']:.2f}  (unlevered, 100% capital — the bar to clear)")

    rows = []
    jobs = [(tf, n) for tf in TIMEFRAMES for n in REGISTRY]
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_job, tf, n): (tf, n) for tf, n in jobs}
        for k, f in enumerate(as_completed(futs), 1):
            rows += f.result()
            if k % 100 == 0:
                print(f"    {k}/{len(jobs)} runs  ({time.perf_counter()-t0:.0f}s)")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"btc_screen_{date.today()}.csv"
    res.to_csv(out, index=False)
    print(f"\n  {len(res):,} combinations with enough trades in both halves")

    surv = res[(res.oos_net_pnl_usd > 0) & (res.oos_profit_factor > 1.0)]
    print(f"  Profitable out-of-sample: {len(surv):,} ({len(surv)/max(len(res),1)*100:.1f}%)")

    print("\n  Survival by timeframe / mode:")
    g = res.assign(ok=(res.oos_net_pnl_usd > 0)).groupby(["timeframe", "mode"]).agg(
        tested=("ok", "size"), profitable_oos=("ok", "sum"),
        median_is_sharpe=("is_sharpe", "median"), median_oos_sharpe=("oos_sharpe", "median"))
    print(g.reindex(TIMEFRAMES, level=0).round(2).to_string())

    cols = ["strategy", "timeframe", "mode", "is_total_trades", "is_sharpe", "is_cagr_pct",
            "is_max_drawdown_pct", "oos_total_trades", "oos_profit_factor", "oos_sharpe",
            "oos_cagr_pct", "oos_max_drawdown_pct", "oos_net_pnl_usd"]

    print("\n" + "=" * 118)
    print("  HONEST VIEW — top 25 chosen by IN-SAMPLE Sharpe, with what they then did OOS")
    print("=" * 118)
    print(res.sort_values("is_sharpe", ascending=False).head(25)[cols].to_string(index=False))

    print("\n" + "=" * 118)
    print("  FOR REFERENCE ONLY (biased) — top 25 by OOS Sharpe")
    print("=" * 118)
    print(surv.sort_values("oos_sharpe", ascending=False).head(25)[cols].to_string(index=False))

    top_is = res.sort_values("is_sharpe", ascending=False).head(25)
    print(f"\n  IS-top-25: median IS Sharpe {top_is.is_sharpe.median():.2f} -> "
          f"median OOS Sharpe {top_is.oos_sharpe.median():.2f}; "
          f"{(top_is.oos_net_pnl_usd > 0).sum()}/25 profitable OOS")
    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.0f}s\n")


if __name__ == "__main__":
    main()
