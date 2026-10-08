import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Portfolio of out-of-sample survivors — $20,000 TOTAL
=======================================================
The four strategies that retained a positive out-of-sample edge are combined
into ONE portfolio sharing a single $20,000 account, so results are directly
comparable with the standalone $20,000 runs.

Allocation: equal weight. Each strategy gets capital/N and risks 1% of its
OWN allocation per trade, so total portfolio risk per trade stays ~1%.

The point of a portfolio is diversification: individually weak strategies can
combine into something better IF their return streams are uncorrelated. The
correlation matrix below is the number that decides whether that is true here.

Run: python backtesting/book_strategies/kaufman/run_portfolio.py
"""

import time
from pathlib import Path
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, get_bars_range
from backtesting.book_strategies.kaufman.harness import run as run_harness, HARNESS_DEFAULTS
from backtesting.book_strategies.kaufman.strategies_adaptive import (
    s088_kama, s092_adaptive_breakout,
)
from backtesting.book_strategies.kaufman.strategies_channels import (
    s019_linreg_breakout, s028_fractal_breakout,
)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

TOTAL_CAPITAL = 20_000          # shared across ALL strategies
RETAIL_COST = 0.35
OOS_START, OOS_END = "2003-05-05", "2019-08-23"

# Out-of-sample survivors only, frozen configs
MEMBERS = [
    ("S019 LinRegBreak", s019_linreg_breakout,   "2h",  {"window": 50, "width_mult": 1.5}),
    ("S092 AdaptiveBrk", s092_adaptive_breakout, "4h",  {"n_bars": 5, "stop_atr_mult": 3.0, "trailing": True}),
    ("S088 KAMA",        s088_kama,              "12h", {"er_period": 10, "slow": 30, "stop_atr_mult": 2.0}),
    ("S028 Fractal",     s028_fractal_breakout,  "2h",  {"fractal_n": 3}),
]


def _daily_pnl(fn, tf, params, capital, loader) -> tuple:
    cfg = {**HARNESS_DEFAULTS, "capital": capital, "risk_pct": 0.01,
           "max_position_pct": 0.50, "mode": "flat", "entry_on": "close",
           "default_stop_atr": 2.0, "cost_per_unit": RETAIL_COST, **params}
    df = loader(tf)
    sig = fn(df.copy(), cfg)
    trades = run_harness(df, sig, cfg)
    if not trades:
        return pd.Series(dtype=float), 0
    t = pd.DataFrame([x.to_dict() for x in trades])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    return t.groupby(t["exit_time"].dt.date)["pnl"].sum(), len(t)


def _metrics_from_daily(daily: pd.Series, capital: float) -> dict:
    if daily.empty:
        return {}
    equity = capital + daily.cumsum()
    peak = equity.cummax()
    dd = ((equity - peak) / peak * 100).min()

    d0, d1 = min(daily.index), max(daily.index)
    years = max((d1 - d0).days, 1) / 365.25
    net = daily.sum()
    final = capital + net
    cagr = ((final / capital) ** (1 / years) - 1) * 100 if final > 0 else -100.0
    sharpe = (daily.mean() / daily.std()) * np.sqrt(252) if daily.std() > 0 else 0.0

    return {"net_pnl_usd": net, "final_equity_usd": final, "cagr_pct": cagr,
            "sharpe": sharpe, "max_drawdown_pct": dd, "years": years}


def analyse(label: str, loader) -> None:
    n = len(MEMBERS)
    per = TOTAL_CAPITAL / n

    print("\n" + "=" * 104)
    print(f"  {label}")
    print(f"  ${TOTAL_CAPITAL:,} TOTAL shared across {n} strategies "
          f"(${per:,.0f} each, 1% risk of own allocation)")
    print("=" * 104)

    streams, rows = {}, []
    for name, fn, tf, params in MEMBERS:
        daily, ntr = _daily_pnl(fn, tf, params, per, loader)
        streams[name] = daily
        m = _metrics_from_daily(daily, per)
        rows.append({"strategy": name, "timeframe": tf, "trades": ntr, "alloc": per, **m})

    print(f"\n  Standalone contribution (each on its ${per:,.0f} slice)")
    print(f"  {'strategy':<20}{'TF':>5}{'trades':>8}{'CAGR':>9}{'Sharpe':>9}{'MaxDD':>9}"
          f"{'net $':>11}{'final $':>11}")
    print("  " + "-" * 82)
    for r in rows:
        print(f"  {r['strategy']:<20}{r['timeframe']:>5}{r['trades']:>8}"
              f"{r.get('cagr_pct',0):>8.2f}%{r.get('sharpe',0):>9.3f}"
              f"{r.get('max_drawdown_pct',0):>8.2f}%{r.get('net_pnl_usd',0):>11,.0f}"
              f"{r.get('final_equity_usd',0):>11,.0f}")

    # ── Combined portfolio ───────────────────────────────────────────────
    all_days = sorted(set().union(*[set(s.index) for s in streams.values() if not s.empty]))
    aligned = pd.DataFrame({k: v.reindex(all_days, fill_value=0.0)
                            for k, v in streams.items()}).sort_index()
    port_daily = aligned.sum(axis=1)
    pm = _metrics_from_daily(port_daily, TOTAL_CAPITAL)

    print(f"\n  {'PORTFOLIO (combined)':<20}{'':>5}"
          f"{sum(r['trades'] for r in rows):>8}"
          f"{pm['cagr_pct']:>8.2f}%{pm['sharpe']:>9.3f}"
          f"{pm['max_drawdown_pct']:>8.2f}%{pm['net_pnl_usd']:>11,.0f}"
          f"{pm['final_equity_usd']:>11,.0f}")

    best = max(rows, key=lambda r: r.get("sharpe", -99))
    print(f"\n  Best single member (Sharpe): {best['strategy']} "
          f"Sharpe {best.get('sharpe',0):.3f} vs portfolio {pm['sharpe']:.3f}"
          f"   →  {'PORTFOLIO WINS' if pm['sharpe'] > best.get('sharpe',0) else 'no diversification gain'}")

    # Scale-free comparison: what would $20k in the single best strategy do?
    print(f"  (Note: each member above runs on only ${per:,.0f}; the portfolio's "
          f"advantage is risk-adjusted, not absolute.)")

    # ── Correlation — the number that decides if this is worth doing ─────
    print(f"\n  DAILY P&L CORRELATION  (low = genuine diversification)")
    corr = aligned.corr()
    print(corr.round(3).to_string())
    off = corr.to_numpy()[np.triu_indices(len(corr), k=1)]
    print(f"\n  average pairwise correlation: {off.mean():.3f}   "
          f"(max {off.max():.3f}, min {off.min():.3f})")

    # ── Is equal-CAPITAL the wrong weighting? ────────────────────────────
    # Equal capital ≠ equal risk. A strategy that trades 3x as often at 1%
    # risk contributes far more volatility, so it can dominate portfolio risk
    # while contributing less return. Re-weight by inverse volatility.
    vols = aligned.std()
    inv = (1.0 / vols).replace([np.inf, -np.inf], np.nan).dropna()
    weights = inv / inv.sum()
    rp_daily = (aligned[weights.index] * (weights * n)).sum(axis=1)
    rp = _metrics_from_daily(rp_daily, TOTAL_CAPITAL)

    print(f"\n  Capital weights — equal vs inverse-volatility (risk parity)")
    for k in weights.index:
        print(f"    {k:<20} equal {1/n*100:>5.1f}%   inv-vol {weights[k]*100:>5.1f}%"
              f"   (${TOTAL_CAPITAL*weights[k]:>7,.0f})")

    print(f"\n  {'portfolio variant':<28}{'CAGR':>9}{'Sharpe':>9}{'MaxDD':>9}{'net $':>11}{'final $':>11}")
    print("  " + "-" * 77)
    print(f"  {'equal capital':<28}{pm['cagr_pct']:>8.2f}%{pm['sharpe']:>9.3f}"
          f"{pm['max_drawdown_pct']:>8.2f}%{pm['net_pnl_usd']:>11,.0f}{pm['final_equity_usd']:>11,.0f}")
    print(f"  {'inverse-volatility':<28}{rp['cagr_pct']:>8.2f}%{rp['sharpe']:>9.3f}"
          f"{rp['max_drawdown_pct']:>8.2f}%{rp['net_pnl_usd']:>11,.0f}{rp['final_equity_usd']:>11,.0f}")

    # Best member scaled to the FULL account — the real benchmark
    best_name = max(rows, key=lambda r: r.get("sharpe", -99))["strategy"]
    solo = streams[best_name] * n          # same strategy on all $20,000
    sm = _metrics_from_daily(solo, TOTAL_CAPITAL)
    print(f"  {best_name + ' alone on $20k':<28}{sm['cagr_pct']:>8.2f}%{sm['sharpe']:>9.3f}"
          f"{sm['max_drawdown_pct']:>8.2f}%{sm['net_pnl_usd']:>11,.0f}{sm['final_equity_usd']:>11,.0f}")

    return aligned, pm


def main():
    t0 = time.perf_counter()
    print("=" * 104)
    print("  PORTFOLIO OF OUT-OF-SAMPLE SURVIVORS")
    print("=" * 104)

    a_is, m_is = analyse("IN-SAMPLE  2019-08 → 2026-08 (7y)  [optimistic — fitted on this data]",
                         lambda tf: get_bars(tf))
    a_oos, m_oos = analyse("OUT-OF-SAMPLE  2003-05 → 2019-08 (16y)  [the honest number]",
                           lambda tf: get_bars_range(OOS_START, OOS_END, tf, "oos"))

    print("\n" + "=" * 104)
    print("  SUMMARY")
    print("=" * 104)
    print(f"  {'window':<28}{'CAGR':>9}{'Sharpe':>9}{'MaxDD':>9}{'net $':>12}{'final $':>12}")
    print("  " + "-" * 79)
    print(f"  {'in-sample (7y)':<28}{m_is['cagr_pct']:>8.2f}%{m_is['sharpe']:>9.3f}"
          f"{m_is['max_drawdown_pct']:>8.2f}%{m_is['net_pnl_usd']:>12,.0f}{m_is['final_equity_usd']:>12,.0f}")
    print(f"  {'OUT-OF-SAMPLE (16y)':<28}{m_oos['cagr_pct']:>8.2f}%{m_oos['sharpe']:>9.3f}"
          f"{m_oos['max_drawdown_pct']:>8.2f}%{m_oos['net_pnl_usd']:>12,.0f}{m_oos['final_equity_usd']:>12,.0f}")

    a_oos.to_csv(RESULTS_DIR / f"kaufman_portfolio_oos_daily_{date.today()}.csv")
    print(f"\n  Runtime: {time.perf_counter()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
