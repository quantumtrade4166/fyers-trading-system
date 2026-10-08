# ============================================================
# etf/etf_loader.py
# Fast loader for the daily ETF parquets in "ETF data/".
#
#   from etf.etf_loader import ETFLoader
#   loader = ETFLoader()
#   df    = loader.load("NIFTYBEES")                      # full history
#   df    = loader.load("GOLDBEES", start="2015-01-01")
#   panel = loader.load_many(["NIFTYBEES", "GOLDBEES"])   # dict of frames
#   px    = loader.close_panel(["NIFTYBEES", "GOLDBEES"]) # aligned close matrix
# ============================================================

import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).parent.parent
ETF_DIR = BASE_DIR / "ETF data"


class ETFLoader:
    def __init__(self, data_dir: Path | str = ETF_DIR):
        self.data_dir = Path(data_dir)
        if not self.data_dir.exists():
            raise FileNotFoundError(f"ETF data directory not found: {self.data_dir}")

    # ── discovery ────────────────────────────────────────────

    def available(self) -> list[str]:
        """Short names of every ETF on disk, e.g. ['ALPHA', 'AUTOBEES', ...]."""
        return sorted(p.stem for p in self.data_dir.glob("*.parquet")
                      if not p.stem.startswith("_"))

    def manifest(self) -> pd.DataFrame:
        """Coverage + liquidity summary written by download_etf_daily.py."""
        path = self.data_dir / "_manifest.csv"
        if not path.exists():
            raise FileNotFoundError(f"No manifest yet — run download_etf_daily.py --manifest-only")
        return pd.read_csv(path)

    def liquid(self, min_turnover: float = 1e7, min_years: float = 3.0) -> list[str]:
        """
        ETFs worth trading: median 60-day turnover above `min_turnover`
        (default ₹1 crore/day) and at least `min_years` of history.
        """
        man = self.manifest()
        sel = man[(man["median_turnover_60d"] >= min_turnover) &
                  (man["years"] >= min_years)]
        return [s.replace("NSE:", "").replace("-EQ", "") for s in sel["symbol"]]

    # ── loading ──────────────────────────────────────────────

    def load(self, name: str, start: str | None = None,
             end: str | None = None) -> pd.DataFrame:
        """One ETF as a date-indexed OHLCV frame."""
        name = name.replace("NSE:", "").replace("-EQ", "")
        path = self.data_dir / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"No data for {name} in {self.data_dir}")

        df = pd.read_parquet(path).sort_index()
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        return df

    def load_many(self, names: list[str], start: str | None = None,
                  end: str | None = None) -> dict[str, pd.DataFrame]:
        out = {}
        for n in names:
            try:
                out[n] = self.load(n, start, end)
            except FileNotFoundError:
                continue
        return out

    def close_panel(self, names: list[str], start: str | None = None,
                    end: str | None = None) -> pd.DataFrame:
        """
        Aligned close-price matrix (rows = dates, cols = ETFs).
        Dates are the union across symbols; missing values stay NaN so a
        strategy can decide for itself whether to forward-fill.
        """
        frames = {n: df["close"].rename(n)
                  for n, df in self.load_many(names, start, end).items()}
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames.values(), axis=1).sort_index()


if __name__ == "__main__":
    loader = ETFLoader()
    names = loader.available()
    print(f"{len(names)} ETFs available in {loader.data_dir}\n")
    for n in names:
        df = loader.load(n)
        print(f"  {n:<14} {len(df):>5} bars  "
              f"{df.index.min().date()} -> {df.index.max().date()}")
