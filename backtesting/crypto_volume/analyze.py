import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Summarise a screen: does volume add anything, and what survives out-of-sample?

Per (strategy, use_volume): median Sharpe over the WHOLE param grid (robust,
no cherry-picking), plus the config with the best IN-SAMPLE Sharpe and what it
then did OUT-OF-SAMPLE (2024+) — the honest number.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.analyze --tag v1
"""

import argparse
from pathlib import Path

import pandas as pd

RES = Path(__file__).parent / "results"


def summarise(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for (strat, uv), g in df.groupby(["strategy", "use_volume"], sort=False):
        best = g.loc[g["sharpe_is"].idxmax()]
        out.append({
            "strategy": strat, "volume": "VOL" if uv else "base", "n_cfg": len(g),
            "med_S": g["sharpe"].median(), "med_IS": g["sharpe_is"].median(),
            "med_OOS": g["sharpe_oos"].median(), "med_25_26": g["sharpe_2025_26"].median(),
            "pct_OOS>0": (g["sharpe_oos"] > 0).mean(),
            "bestIS_params": best["params"], "bestIS_IS": best["sharpe_is"],
            "bestIS_OOS": best["sharpe_oos"], "bestIS_25_26": best["sharpe_2025_26"],
            "bestIS_DD20": best["maxdd_at20vol"], "long_share": g["long_share"].median(),
            "cost_share": g["cost_share"].median(),
        })
    return pd.DataFrame(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v1")
    args = ap.parse_args()
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    for tf in ("1d", "4h"):
        f = RES / f"screen_{tf}_{args.tag}.csv"
        if not f.exists():
            continue
        s = summarise(pd.read_csv(f))
        s.to_csv(RES / f"summary_{tf}_{args.tag}.csv", index=False)
        print(f"\n=== {tf} ===")
        print(s.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
