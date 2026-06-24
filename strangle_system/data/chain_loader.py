"""
strangle_system/data/chain_loader.py
=====================================
Read option-chain snapshots back for signal computation and backtesting.

Mirrors backtesting.data_loader.DataLoader conventions. The key extra here is
POINT-IN-TIME correctness: `snapshot_asof(underlying, asof)` returns the most
recent snapshot on or before `asof` — never a future one. A signal computed for
date T may only use the snapshot captured at T's close or earlier.

Snapshot layout:
    data/chain_snapshots/{UNDERLYING}/{YYYY-MM-DD}.parquet
Columns: chain_collector.SNAPSHOT_COLUMNS
"""

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Union

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.append(str(_PROJECT_ROOT))

from strangle_system import config
from strangle_system.data.chain_collector import SNAPSHOT_COLUMNS


def _as_date(d: Union[str, date, datetime]) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return pd.Timestamp(d).date()


class ChainLoader:
    """Loader for daily option-chain snapshots."""

    def __init__(self, snapshot_dir: Optional[Union[str, Path]] = None):
        self.dir = Path(snapshot_dir) if snapshot_dir else config.CHAIN_SNAPSHOT_DIR

    # ── Discovery ───────────────────────────────────────────────────────────
    def available_underlyings(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(p.name for p in self.dir.iterdir() if p.is_dir())

    def available_dates(self, underlying: str) -> list[date]:
        """Sorted list of snapshot dates on disk for an underlying."""
        folder = self.dir / underlying
        if not folder.exists():
            return []
        out = []
        for p in folder.glob("*.parquet"):
            try:
                out.append(pd.Timestamp(p.stem).date())
            except Exception:
                continue
        return sorted(out)

    # ── Single-day load ─────────────────────────────────────────────────────
    def load_snapshot(self, underlying: str,
                      day: Union[str, date, datetime]) -> Optional[pd.DataFrame]:
        """Load the snapshot for an exact date. None if absent."""
        d = _as_date(day)
        path = self.dir / underlying / f"{d}.parquet"
        if not path.exists():
            return None
        df = pd.read_parquet(path)
        return df.reindex(columns=SNAPSHOT_COLUMNS) if not df.empty else df

    def snapshot_asof(self, underlying: str,
                      asof: Union[str, date, datetime]) -> Optional[pd.DataFrame]:
        """
        POINT-IN-TIME: most recent snapshot on or before `asof`.
        Returns None if no snapshot exists at/before that date.
        """
        target = _as_date(asof)
        candidates = [d for d in self.available_dates(underlying) if d <= target]
        if not candidates:
            return None
        return self.load_snapshot(underlying, max(candidates))

    def latest(self, underlying: str) -> Optional[pd.DataFrame]:
        dates = self.available_dates(underlying)
        return self.load_snapshot(underlying, dates[-1]) if dates else None

    def load_range(self, underlying: str,
                   start: Union[str, date, datetime],
                   end: Union[str, date, datetime]) -> pd.DataFrame:
        """Concatenate all snapshots in [start, end]."""
        s, e = _as_date(start), _as_date(end)
        frames = [self.load_snapshot(underlying, d)
                  for d in self.available_dates(underlying) if s <= d <= e]
        frames = [f for f in frames if f is not None and not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=SNAPSHOT_COLUMNS)

    # ── Convenience extractors ──────────────────────────────────────────────
    @staticmethod
    def nearest_expiry(snapshot: pd.DataFrame) -> Optional[str]:
        """The closest expiry present in a snapshot (string YYYY-MM-DD)."""
        exps = sorted(e for e in snapshot["expiry"].dropna().unique())
        return exps[0] if exps else None

    @staticmethod
    def expiry_slice(snapshot: pd.DataFrame, expiry: Optional[str] = None) -> pd.DataFrame:
        """Rows for one expiry (default = nearest)."""
        if expiry is None:
            expiry = ChainLoader.nearest_expiry(snapshot)
        return snapshot[snapshot["expiry"] == expiry].copy()

    @staticmethod
    def spot(snapshot: pd.DataFrame) -> Optional[float]:
        s = snapshot["spot"].dropna()
        return float(s.iloc[0]) if len(s) else None


if __name__ == "__main__":
    config.reconfigure_stdout()
    cl = ChainLoader()
    us = cl.available_underlyings()
    print("Underlyings with snapshots:", us or "(none yet — run chain_collector)")
    for u in us:
        ds = cl.available_dates(u)
        print(f"  {u}: {len(ds)} snapshots", f"({ds[0]} → {ds[-1]})" if ds else "")
        snap = cl.latest(u)
        if snap is not None and not snap.empty:
            print(f"    latest spot={cl.spot(snap)} nearest_expiry={cl.nearest_expiry(snap)} rows={len(snap)}")
