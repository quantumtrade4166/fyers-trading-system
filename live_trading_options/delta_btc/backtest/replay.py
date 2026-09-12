"""
backtest/replay.py — feed the archived chain back through the REAL controller.
==============================================================================

Delta serves candles for live contracts only, so the only history that exists is
what the collector captured: a full BTC option chain snapshot roughly once a
minute, in parquet. This turns that back into something the strategy can trade.

THE POINT IS THAT IT IS THE SAME CODE
`ReplayChain` implements exactly the surface `LiveChain` exposes — the same one
the dry-run's FakeChain proved is the whole contract between chain and strategy.
So `BTCController` is driven here byte-for-byte as it runs live: same selector,
same windows, same stop logic, same fills at the touch, same fees. A backtest
that re-implemented the strategy would only ever test the re-implementation.

WHAT IS FAITHFUL, AND WHAT IS NOT

Faithful:
  - real bid/ask at every minute, so fills cross a real spread
  - real marks, so stops trigger where they would have
  - real strike grid, so the selector picks from what actually existed
  - fees on every fill, same model as live

Not faithful, and it matters:
  - the archive is ~1 sample a minute; the live engine polls every 5s and reads a
    push feed sub-second. A stop is therefore detected LATER here than live, and
    fills at whatever the next minute shows. On a fast move that overstates the
    loss. Backtested stop-outs are pessimistic by construction.
  - our own orders never move the book. At 1000 contracts against ~10,000 resting
    that is close to true, but it is still an assumption.
  - gaps in the archive are skipped, not interpolated. A day with a hole is a day
    the strategy simply did not see; `coverage` reports it per day so a thin day
    can be excluded rather than quietly averaged in.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import datetime as dt

import pandas as pd

from core.selector import atm_strike, CE, PE
from core.shared import ARCHIVE

CONTRACT_VALUE = 0.001


class ReplayChain:
    """One expiry at one instant, wearing LiveChain's interface."""

    def __init__(self, expiry: str, span: int = 20, settle_ist: str = "17:30"):
        self.expiry = expiry
        self.span = span
        self.settle_ist = settle_ist
        self.contract_value = CONTRACT_VALUE
        self.spot = None
        self.atm = None
        self.strikes: list = []
        self.mark: dict = {}
        self.bid: dict = {}
        self.ask: dict = {}
        self.updated = None

    def load_snapshot(self, rows) -> int:
        """Absorb one minute of the archive: rows for THIS expiry only."""
        self.mark, self.bid, self.ask = {}, {}, {}
        strikes = set()
        spot = None
        for r in rows:
            key = (float(r["strike"]), r["opt_type"])
            strikes.add(float(r["strike"]))
            if r["mark"] is not None and r["mark"] == r["mark"]:
                self.mark[key] = float(r["mark"])
            # An empty side is LEFT OUT, not carried over: a fill simulated against
            # a bid that was not there is a fill that never happened.
            if r["best_bid"] is not None and r["best_bid"] == r["best_bid"]:
                self.bid[key] = float(r["best_bid"])
            if r["best_ask"] is not None and r["best_ask"] == r["best_ask"]:
                self.ask[key] = float(r["best_ask"])
            if r["spot"] is not None and r["spot"] == r["spot"]:
                spot = float(r["spot"])
            if r.get("contract_value"):
                self.contract_value = float(r["contract_value"])
        self.strikes = sorted(strikes)
        if spot is not None:
            self.spot = spot
            self.atm = atm_strike(spot, self.strikes)
        return len(self.mark)

    # ── the LiveChain surface the controller uses ────────────────────────
    def _band(self):
        if self.atm not in self.strikes:
            return (self.strikes[0], self.strikes[-1]) if self.strikes else (0, 0)
        i = self.strikes.index(self.atm)
        return (self.strikes[max(0, i - self.span)],
                self.strikes[min(len(self.strikes) - 1, i + self.span)])

    def chain(self) -> dict:
        if self.atm is None or not self.strikes:
            return dict(self.mark)
        lo, hi = self._band()
        return {k: v for k, v in self.mark.items() if lo <= k[0] <= hi}

    def contract(self, strike, opt_type):
        strike = float(strike)
        if strike not in self.strikes:
            return None
        code = "C" if opt_type == CE else "P"
        return {"symbol": f"{code}-BTC-{strike:.0f}-{self.expiry}",
                "product_id": None, "contract_value": self.contract_value,
                "tick_size": 0.1}

    def symbol_for(self, strike, opt_type):
        c = self.contract(strike, opt_type)
        return c["symbol"] if c else None

    def fill_price(self, strike, opt_type, side):
        key = (float(strike), opt_type)
        book = self.bid if side == "SELL" else self.ask
        p = book.get(key)
        if p is not None:
            return p, True
        return self.mark.get(key), False

    def is_ready(self) -> bool:
        if self.spot is None or self.atm is None:
            return False
        ce = sum(1 for (_, t) in self.mark if t == CE)
        pe = sum(1 for (_, t) in self.mark if t == PE)
        return ce >= 3 and pe >= 3

    def seconds_to_settlement(self, now: dt.datetime) -> int:
        y, m, d = 2000 + int(self.expiry[4:6]), int(self.expiry[2:4]), int(self.expiry[0:2])
        h, mi = (int(x) for x in self.settle_ist.split(":"))
        return int((dt.datetime(y, m, d, h, mi) - now).total_seconds())


# ── loading the archive ──────────────────────────────────────────────────
_COLS = ["captured", "symbol", "opt_type", "strike", "expiry", "spot", "mark",
         "best_bid", "best_ask", "contract_value"]


def load_days(days, archive: Path = None) -> pd.DataFrame:
    """Every archived snapshot for the given dates, one row per contract-minute.

    Snapshots are rounded to the MINUTE and de-duplicated: the collector polls
    every 60s but drifts, so two samples can land in the same minute and the
    strategy would then see the same minute twice.
    """
    archive = Path(archive or ARCHIVE)
    frames = []
    for day in days:
        d = archive / str(day)
        if not d.exists():
            continue
        for f in sorted(d.glob("*.parquet")):
            try:
                frames.append(pd.read_parquet(f, columns=_COLS))
            except Exception as e:
                print(f"  !! unreadable {f.name}: {e}")
    if not frames:
        return pd.DataFrame(columns=_COLS)
    df = pd.concat(frames, ignore_index=True)
    df["minute"] = pd.to_datetime(df["captured"]).dt.floor("min")
    df = df.sort_values("captured").drop_duplicates(["minute", "symbol"], keep="last")
    return df


def available_days(archive: Path = None) -> list:
    archive = Path(archive or ARCHIVE)
    return sorted(p.name for p in archive.iterdir()
                  if p.is_dir() and len(p.name) == 10)


def coverage(df: pd.DataFrame) -> dict:
    """Minutes captured per day — how much of each day the strategy actually saw."""
    out = {}
    for day, g in df.groupby(df["minute"].dt.date):
        mins = g["minute"].nunique()
        out[str(day)] = {"minutes": mins, "pct_of_day": round(100 * mins / 1440, 1),
                         "first": str(g["minute"].min())[11:16],
                         "last": str(g["minute"].max())[11:16]}
    return out
