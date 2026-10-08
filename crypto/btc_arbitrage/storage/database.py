"""
storage/database.py — one-second spread history in daily SQLite files.
=====================================================================

Binance pushes tens of book updates a second, so storing every recompute would
be ~2 million rows a day of near-duplicates. Instead the monitor folds each
second into ONE row that keeps the LAST values (for a continuous line) AND the
best of each direction inside that second (s1_max / s2_max / net1_max /
net2_max), so a 200 ms blip is never lost to the downsampling.

    data/spread_db/YYYY-MM-DD.sqlite      (IST date, WAL mode — the dashboard
                                            reads while the monitor writes)

Rows: ~86,400 a day, ~12 MB. Files older than RETENTION_DAYS are deleted.

`read_series()` is what the dashboard API calls; it never writes.
"""

import sqlite3
import datetime as dt
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

COLUMNS = [
    ("t", "INTEGER PRIMARY KEY"),           # epoch seconds (UTC)
    ("d_bid", "REAL"), ("d_ask", "REAL"), ("d_bid_qty", "REAL"), ("d_ask_qty", "REAL"),
    ("b_bid", "REAL"), ("b_ask", "REAL"), ("b_bid_qty", "REAL"), ("b_ask_qty", "REAL"),
    ("d_mid", "REAL"), ("b_mid", "REAL"),
    ("mid_spread", "REAL"), ("mid_spread_pct", "REAL"),
    ("mid_min", "REAL"), ("mid_max", "REAL"),
    ("s1", "REAL"), ("s2", "REAL"),          # last gross per direction
    ("s1_max", "REAL"), ("s2_max", "REAL"),  # best gross per direction in the second
    ("net1_max", "REAL"), ("net2_max", "REAL"),
    ("direction", "TEXT"), ("gross_spread", "REAL"), ("fees", "REAL"),
    ("net_spread", "REAL"), ("spread_pct", "REAL"),
    ("edge", "REAL"), ("edge_net", "REAL"),
    ("quality", "TEXT"), ("opportunity", "INTEGER"),
    ("d_age_ms", "REAL"), ("b_age_ms", "REAL"),
    ("d_latency_ms", "REAL"), ("b_latency_ms", "REAL"),
    ("usdc_usdt", "REAL"), ("n", "INTEGER"),
    # depth-priced spreads for PAPER_SIZE_BTC (added 2026-10-04; older rows NULL)
    ("s1_1btc", "REAL"), ("s2_1btc", "REAL"), ("s1_1btc_max", "REAL"), ("s2_1btc_max", "REAL"),
    ("d_depth_age_ms", "REAL"), ("b_depth_age_ms", "REAL"),
]
NAMES = [c for c, _ in COLUMNS]


def ist_date(epoch_s: float) -> str:
    return dt.datetime.fromtimestamp(epoch_s, IST).strftime("%Y-%m-%d")


def _path(db_dir: Path, day: str) -> Path:
    return Path(db_dir) / f"{day}.sqlite"


class SpreadStore:
    """Writer side. One connection, reopened when the IST date rolls."""

    def __init__(self, db_dir: Path, retention_days: int = 90):
        self.dir = Path(db_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._day = None
        self._con = None

    def _open(self, day: str):
        if self._con:
            self._con.close()
        self._con = sqlite3.connect(_path(self.dir, day), timeout=10)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        cols = ", ".join(f"{c} {t}" for c, t in COLUMNS)
        self._con.execute(f"CREATE TABLE IF NOT EXISTS snap ({cols})")
        have = {r[1] for r in self._con.execute("PRAGMA table_info(snap)")}
        for c, t in COLUMNS:                    # files created before a column was added
            if c not in have:
                self._con.execute(f"ALTER TABLE snap ADD COLUMN {c} {t}")
        self._con.commit()
        self._day = day
        self.prune()

    def write(self, row: dict):
        day = ist_date(row["t"])
        if day != self._day:
            self._open(day)
        vals = [row.get(c) for c in NAMES]
        self._con.execute(f"INSERT OR REPLACE INTO snap ({','.join(NAMES)}) "
                          f"VALUES ({','.join('?' * len(NAMES))})", vals)
        self._con.commit()

    def prune(self):
        cutoff = (dt.datetime.now(IST) - dt.timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        for f in self.dir.glob("*.sqlite"):
            if f.stem < cutoff:
                for g in (f, f.with_suffix(".sqlite-wal"), f.with_suffix(".sqlite-shm")):
                    try:
                        g.unlink()
                    except OSError:
                        pass

    def close(self):
        if self._con:
            self._con.close()
            self._con = None


# ── reader side (dashboard) ──────────────────────────────────────────────
SERIES_COLS = ("t", "mid_spread", "s1", "s2", "s1_max", "s2_max", "net1_max", "net2_max",
               "fees", "edge_net", "quality", "d_mid", "b_mid")


def read_series(db_dir: Path, start_s: int, end_s: int, max_points: int = 1500) -> dict:
    """Rows in [start_s, end_s], downsampled to <= max_points buckets.

    Per bucket: the LAST mid / s1 / s2 (the line stays continuous and true to
    what was on screen) and the BEST s1 / s2 / net inside the bucket (so a
    short-lived opportunity still shows up at the 1-day zoom)."""
    span = max(1, end_s - start_s)
    bucket = max(1, -(-span // max_points))     # ceil
    days = sorted({ist_date(start_s), ist_date(end_s)})
    out = []
    for day in days:
        p = _path(db_dir, day)
        if not p.exists():
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            if bucket == 1:
                q = (f"SELECT {','.join(SERIES_COLS)} FROM snap WHERE t BETWEEN ? AND ? ORDER BY t")
                rows = con.execute(q, (start_s, end_s)).fetchall()
            else:
                # SQLite: bare columns next to MAX(t) come from the row holding that max
                q = ("SELECT MAX(t), mid_spread, s1, s2, MAX(s1_max), MAX(s2_max), "
                     "MAX(net1_max), MAX(net2_max), fees, edge_net, quality, d_mid, b_mid "
                     "FROM snap WHERE t BETWEEN ? AND ? GROUP BY t / ? ORDER BY 1")
                rows = con.execute(q, (start_s, end_s, bucket)).fetchall()
        except sqlite3.OperationalError:
            rows = []                           # file created, table not yet
        finally:
            con.close()
        out.extend(dict(zip(SERIES_COLS, r)) for r in rows)
    return {"bucket_s": bucket, "points": out}


def available_days(db_dir: Path) -> list:
    d = Path(db_dir)
    return sorted((f.stem for f in d.glob("*.sqlite")), reverse=True) if d.exists() else []
