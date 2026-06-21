import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from pathlib import Path
from strategy_55day_breakout import load_symbol, run_backtest, print_results, DATA_DIR

def get_all_symbols() -> list[str]:
    return [p.stem for p in DATA_DIR.glob("*.parquet")]

if __name__ == "__main__":
    symbols = get_all_symbols()
    print(f"Loading {len(symbols)} symbols from Nifty 500 Daily Data...")
    result = run_backtest(symbols, verbose=False)
    print_results(result)

    # Save trades to CSV
    if not result["trades"].empty:
        out = Path(__file__).parent / "results"
        out.mkdir(exist_ok=True)
        result["trades"].to_csv(out / "trades_55day_v1.csv", index=False)
        result["equity"].to_csv(out / "equity_55day_v1.csv")
        print(f"  Trades saved → results/trades_55day_v1.csv")
        print(f"  Equity saved → results/equity_55day_v1.csv")
