import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Top-N crypto universe for the volume-strategy backtests
=======================================================
CoinGecko market-cap ranking (today) -> drop stablecoins / gold / tokenised
funds -> keep coins with a Binance USDT-M perpetual (incl. the 1000x contracts
like 1000PEPE) -> first N.

SURVIVORSHIP WARNING: this is TODAY's top N. Coins that fell out of the list
are missing, which flatters long-side results. Backtests must report it.

Output: crypto/universe/universe_top{N}.csv  (rank, coin, name, perp, onboard)
Run   : .venv\\Scripts\\python.exe crypto\\universe\\build_universe.py --n 100
"""

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent

# Not directional crypto risk: stablecoins, gold, tokenised T-bills/credit.
EXCLUDE = {
    "USDT", "USDC", "USDS", "USDE", "DAI", "USD1", "USDG", "PYUSD", "RLUSD", "USYC",
    "USDY", "BUIDL", "USDF", "USDD", "BFUSD", "USDGO", "OUSD", "USTB", "GHO", "FDUSD",
    "TUSD", "USDP", "USD0", "EURC", "EURSAFO", "U", "FIGR_HELOC", "XAUT", "PAXG",
    "BCAP", "SUSDE", "SUSDS", "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "CBBTC",
    "WBETH", "RETH", "METH", "LBTC", "SOLVBTC", "BNSOL", "JITOSOL", "MSOL", "CLBTC",
}


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "fyers-pipeline/universe"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    args = ap.parse_args()

    coins = []
    for page in (1, 2):
        coins += _get("https://api.coingecko.com/api/v3/coins/markets"
                      f"?vs_currency=usd&order=market_cap_desc&per_page=250&page={page}")
    ex = _get("https://fapi.binance.com/fapi/v1/exchangeInfo")["symbols"]
    perps = {s["symbol"]: s for s in ex
             if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"
             and s["status"] == "TRADING"}
    (HERE / "coingecko_top500.json").write_text(json.dumps(coins))

    rows, seen = [], set()
    for c in coins:
        sym = c["symbol"].upper()
        if sym in EXCLUDE or sym in seen:
            continue
        perp = next((p for p in (f"{sym}USDT", f"1000{sym}USDT", f"1000000{sym}USDT")
                     if p in perps), None)
        if perp is None:
            continue
        seen.add(sym)
        rows.append({
            "mcap_rank": c["market_cap_rank"], "coin": sym, "name": c["name"],
            "perp": perp,
            "onboard": datetime.fromtimestamp(perps[perp]["onboardDate"] / 1000,
                                              tz=timezone.utc).date(),
        })
        if len(rows) == args.n:
            break

    df = pd.DataFrame(rows)
    out = HERE / f"universe_top{args.n}.csv"
    df.to_csv(out, index=False)
    print(df.to_string(index=False))
    print(f"\n{len(df)} coins -> {out}")
    print("perp listed by year:", df["onboard"].map(lambda d: d.year).value_counts().sort_index().to_dict())


if __name__ == "__main__":
    main()
