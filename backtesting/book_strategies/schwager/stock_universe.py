"""NSE stock symbols from the bhavcopy archive, with every ETF removed
(the 342 names in the `ETF data` folder plus obvious ETF / liquid-fund tickers)."""
import re
from pathlib import Path
from backtesting.engine import bhav

_ETF_DIR = Path(__file__).resolve().parents[3] / "ETF data"
_PAT = re.compile(r"(BEES|ETF|LIQUID|IETF|CASE$|ADD$|^GOLD1|^SILVER1)")


def stock_symbols():
    etfs = {p.stem.upper() for p in _ETF_DIR.glob("*.parquet")}
    return [s for s in bhav.list_symbols() if s.upper() not in etfs and not _PAT.search(s.upper())]
