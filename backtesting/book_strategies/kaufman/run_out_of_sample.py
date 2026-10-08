import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Out-of-sample validation — the honest test
=============================================
IN-SAMPLE  : 2019-08 → 2026-08 (7y).  Every strategy choice, timeframe and
             parameter was selected on this data. Results here are optimistic
             by construction.
OUT-OF-SAMPLE: 2003-05 → 2019-08 (~16y).  Never touched. Configs are FROZEN
             exactly as chosen — no re-tuning, no second look.

The OOS window is also a much harder regime mix than the in-sample one:
  2003-2011  massive gold bull market
  2011-2015  severe bear market
  2015-2019  rangebound
versus a mostly-uptrending 2019-2026.

Rejected strategies are included as a CONTROL: if mean reversion also fails
out-of-sample, that confirms the in-sample rejection was a real property of
the instrument and not an artifact of the test window.

Run: python backtesting/book_strategies/kaufman/run_out_of_sample.py
"""

import time
from pathlib import Path
from datetime import date

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[3]))

from backtesting.book_strategies.kaufman.data import get_bars, get_bars_range
from backtesting.book_strategies.kaufman.harness import backtest
from backtesting.book_strategies.kaufman.strategies_adaptive import (
    s088_kama, s089_vidya, s090_adaptive_rsi, s092_adaptive_breakout,
)
from backtesting.book_strategies.kaufman.strategies_channels import (
    s019_linreg_breakout, s021_donchian_20_10, s023_keltner_breakout,
    s025_bb_meanrev, s026_atr_bands, s028_fractal_breakout,
)

RESULTS_DIR = Path(r"G:\Trading Brain\results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

CAPITAL = 20_000
RETAIL_COST = 0.35

OOS_START, OOS_END = "2003-05-05", "2019-08-23"

BASE_CFG = {
    "capital": CAPITAL, "risk_pct": 0.01, "max_position_pct": 0.50,
    "mode": "flat", "entry_on": "close", "default_stop_atr": 2.0,
    "cost_per_unit": RETAIL_COST,
}

# FROZEN configs — exactly as selected on in-sample data. Do not re-tune.
CANDIDATES = [
    # (label, fn, timeframe, params, in-sample-verdict)
    ("S092 AdaptiveBrkout", s092_adaptive_breakout, "4h",
     {"n_bars": 5, "stop_atr_mult": 3.0, "trailing": True}, "TOP"),
    ("S088 KAMA",           s088_kama, "12h",
     {"er_period": 10, "slow": 30, "stop_atr_mult": 2.0}, "TOP"),
    ("S028 Fractal",        s028_fractal_breakout, "2h",
     {"fractal_n": 3}, "TOP"),
    ("S089 VIDYA",          s089_vidya, "4h",
     {"short_period": 9, "cmo_period": 20, "stop_atr_mult": 3.0}, "TOP"),
    ("S023 KeltnerBreak",   s023_keltner_breakout, "2h",
     {"ema_period": 40, "mult": 2.5, "trailing": True}, "good"),
    ("S026 ATRBands",       s026_atr_bands, "2h",
     {"ma_period": 20, "band_mult": 2.5}, "good"),
    ("S019 LinRegBreak",    s019_linreg_breakout, "2h",
     {"window": 50, "width_mult": 1.5}, "good"),
    ("S021 Donchian",       s021_donchian_20_10, "2h",
     {"entry_period": 30, "exit_period": 10, "stop_atr_mult": 2.0}, "good"),
    # ── CONTROLS: rejected in-sample. Should also fail OOS. ──
    ("S090 AdaptiveRSI *",  s090_adaptive_rsi, "4h",
     {"base_period": 21, "stop_atr_mult": 3.0}, "REJECTED"),
    ("S025 BBMeanRev *",    s025_bb_meanrev, "2h",
     {"bb_period": 20, "bb_std": 2.5, "rsi_os": 30, "max_bars": 8}, "REJECTED"),
]


def main():
    t0 = time.perf_counter()
    print("=" * 122)
    print("  OUT-OF-SAMPLE VALIDATION — frozen configs on data never used for fitting")
    print(f"  IN-SAMPLE     2019-08 -> 2026-08  (7y)   — used to pick strategies, timeframes, parameters")
    print(f"  OUT-OF-SAMPLE {OOS_START} -> {OOS_END} (~16y) — never seen")
    print(f"  ${CAPITAL:,} start, 1% risk, cost ${RETAIL_COST:.2f}/oz.  * = rejected in-sample (control)")
    print("=" * 122)

    rows = []
    print(f"\n  {'strategy':<22}{'TF':>5} │ {'IN-SAMPLE (7y)':^42} │ {'OUT-OF-SAMPLE (16y)':^42}")
    print(f"  {'':<22}{'':>5} │ {'trades':>7}{'PF':>7}{'Sharpe':>8}{'CAGR':>8}{'net $':>11} │ "
          f"{'trades':>7}{'PF':>7}{'Sharpe':>8}{'CAGR':>8}{'net $':>11}")
    print("  " + "─" * 118)

    for label, fn, tf, params, verdict in CANDIDATES:
        cfg = {**BASE_CFG, **params}

        df_is = get_bars(tf)
        _, mi = backtest(df_is.copy(), fn, cfg)

        df_oos = get_bars_range(OOS_START, OOS_END, tf, "oos")
        _, mo = backtest(df_oos.copy(), fn, cfg)

        rows.append({
            "strategy": label, "timeframe": tf, "in_sample_verdict": verdict,
            **{f"is_{k}": v for k, v in mi.items()},
            **{f"oos_{k}": v for k, v in mo.items()},
        })

        print(f"  {label:<22}{tf:>5} │ "
              f"{mi['total_trades']:>7}{mi['profit_factor']:>7.3f}{mi['sharpe']:>8.3f}"
              f"{mi['cagr_pct']:>7.2f}%{mi['net_pnl_usd']:>11,.0f} │ "
              f"{mo['total_trades']:>7}{mo['profit_factor']:>7.3f}{mo['sharpe']:>8.3f}"
              f"{mo['cagr_pct']:>7.2f}%{mo['net_pnl_usd']:>11,.0f}")

    res = pd.DataFrame(rows)
    out = RESULTS_DIR / f"kaufman_out_of_sample_{date.today()}.csv"
    res.to_csv(out, index=False)

    # ── Verdicts ─────────────────────────────────────────────────────────
    print("\n" + "=" * 122)
    print("  VERDICT — did the edge survive on data it was never fitted to?")
    print("=" * 122)
    for _, r in res.iterrows():
        held = r["oos_net_pnl_usd"] > 0 and r["oos_profit_factor"] > 1.0
        if r["in_sample_verdict"] == "REJECTED":
            tag = ("✓ CONTROL BEHAVED (failed OOS too — rejection was real)"
                   if not held else "⚠ control PASSED OOS — in-sample rejection may be wrong")
        else:
            decay = ((r["oos_cagr_pct"] / r["is_cagr_pct"] - 1) * 100
                     if r["is_cagr_pct"] else 0)
            tag = (f"✓ HELD UP  (OOS CAGR {r['oos_cagr_pct']:.2f}% vs IS {r['is_cagr_pct']:.2f}%, "
                   f"{decay:+.0f}%)" if held
                   else f"✗ FAILED OOS (net ${r['oos_net_pnl_usd']:,.0f}, PF {r['oos_profit_factor']:.3f})")
        print(f"  {r['strategy']:<22} {tag}")

    survivors = res[(res["in_sample_verdict"] != "REJECTED") &
                    (res["oos_net_pnl_usd"] > 0) & (res["oos_profit_factor"] > 1.0)]
    tested = res[res["in_sample_verdict"] != "REJECTED"]
    print(f"\n  {len(survivors)} of {len(tested)} candidate strategies survived out-of-sample.")

    print(f"\n  CSV: {out}")
    print(f"  Runtime: {time.perf_counter()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
