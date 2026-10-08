# ============================================================
# etf/validate_etf_data.py
# Data-quality scan + repair for the daily ETF parquets.
#
# Fyers ETF history carries four defects that will quietly corrupt a
# backtest. This module finds all four and fixes the two that are safe
# to fix automatically:
#
#   1. splits       — a permanent price step (e.g. QUAL30IETF 190 -> 19).
#                     FIXED: pre-split OHLC divided by the factor,
#                     volume multiplied by it, so returns stay continuous.
#   2. bad prints   — a 1-5 day plateau ~10x off that reverts
#                     (an ICICI feed artifact clustered around Apr 2024).
#                     FIXED: the offending bars are dropped.
#   3. constant NAV — LIQUIDBEES and friends sit at a fixed price forever;
#                     their return is paid as dividend units and is simply
#                     not in the price. REPORTED — never trade these off
#                     price returns, model the yield instead.
#   4. junk prints  — a big move on near-zero volume in an illiquid ETF.
#                     REPORTED only: the print is real, the ETF is just
#                     untradeable. Filter by turnover instead.
#
#   python etf/validate_etf_data.py            # report only
#   python etf/validate_etf_data.py --fix      # apply splits + drop bad prints
# ============================================================

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
sys.path.append(str(Path(__file__).parent.parent))

from etf.etf_loader import ETF_DIR  # noqa: E402

STEP = 0.35        # move big enough to be a split or a bad print
PERSIST_LO = 0.70  # post/pre level outside [0.70, 1.40] => the step persisted
PERSIST_HI = 1.40
LEVEL_N = 5        # bars either side used to measure the price level
DEV_HI = 2.5       # bar this far above the local baseline is a bad print
DEV_LO = 0.40
FLAT_TOL = 0.005   # annualised drift below this => constant-NAV fund
JUNK_VOL = 1000    # units traded; below this a big move is noise

OHLC = ["open", "high", "low", "close"]


def _level(close: pd.Series, i: int, before: bool) -> float:
    """Median close over LEVEL_N bars strictly before / after position i."""
    seg = close.iloc[max(0, i - LEVEL_N):i] if before else close.iloc[i + 1:i + 1 + LEVEL_N]
    return float(seg.median()) if len(seg) else float("nan")


def find_splits(df: pd.DataFrame) -> list[tuple[pd.Timestamp, float]]:
    """
    Locate permanent price steps. Returns [(date, factor)] where every bar
    BEFORE `date` must be divided by `factor` to line up with later prices.
    """
    close = df["close"]
    ret = close.pct_change()
    out = []

    for i, (d, r) in enumerate(zip(df.index, ret)):
        if pd.isna(r) or abs(r) < STEP:
            continue
        pre, post = _level(close, i, True), _level(close, i, False)
        if pd.isna(pre) or pd.isna(post) or post == 0:
            continue
        if PERSIST_LO <= post / pre <= PERSIST_HI:
            continue  # level came back — that's a bad print, not a split

        factor = pre / post
        # Real splits are whole-number ratios; snap when we are close.
        for cand in range(2, 101):
            if abs(factor - cand) / cand < 0.05:
                factor = float(cand)
                break
            if abs(factor - 1 / cand) * cand < 0.05:
                factor = 1 / cand
                break
        out.append((d, factor))

    return out


def apply_splits(df: pd.DataFrame, splits) -> pd.DataFrame:
    """Back-adjust every bar before each split so the series is continuous."""
    df = df.copy()
    for d, factor in splits:
        mask = df.index < d
        df.loc[mask, OHLC] = df.loc[mask, OHLC] / factor
        df.loc[mask, "volume"] = (df.loc[mask, "volume"] * factor).round().astype("int64")
    return df


def find_bad_prints(df: pd.DataFrame) -> list[pd.Timestamp]:
    """
    Bars sitting far off the local baseline. Run AFTER split adjustment,
    otherwise the split step itself trips the baseline.
    """
    close = df["close"]
    baseline = close.rolling(21, center=True, min_periods=5).median()
    dev = close / baseline
    return list(dev[(dev > DEV_HI) | (dev < DEV_LO)].index)


def scan(path: Path, fix: bool):
    df = pd.read_parquet(path).sort_index()
    name, issues, changed = path.stem, [], False

    dupes = int(df.index.duplicated().sum())
    if dupes:
        issues.append(("duplicate_dates", f"{dupes} rows"))
        df = df[~df.index.duplicated(keep="first")]
        changed = True

    splits = find_splits(df)
    for d, f in splits:
        issues.append(("split", f"{d.date()} factor {f:g}:1 — pre-split prices adjusted"
                                 if fix else f"{d.date()} factor {f:g}:1 — NOT adjusted"))
    if splits and fix:
        df = apply_splits(df, splits)
        changed = True

    probe = apply_splits(df, []) if fix else apply_splits(df, splits)
    bad = find_bad_prints(probe)
    if bad:
        issues.append(("bad_print", ", ".join(str(d.date()) for d in bad[:6])
                                    + (f" (+{len(bad) - 6} more)" if len(bad) > 6 else "")))
        if fix:
            df = df.drop(index=bad)
            changed = True

    close = df["close"]
    years = max((df.index.max() - df.index.min()).days / 365.25, 0.5)
    drift = abs(close.iloc[-1] / close.iloc[0] - 1) / years
    if drift < FLAT_TOL:
        issues.append(("constant_nav", f"drift {drift:.2%}/yr — return paid as units, not price"))

    # Big moves on almost no volume: real prints, untradeable ETF.
    ret = close.pct_change().abs()
    junk = df.index[(ret > STEP) & (df["volume"] < JUNK_VOL)]
    junk = [d for d in junk if d not in set(bad)]
    if junk:
        issues.append(("junk_print", f"{len(junk)} big moves on <{JUNK_VOL} units traded"))

    zero = int((df["volume"] == 0).sum())
    if zero > len(df) * 0.10:
        issues.append(("volume_missing", f"{zero}/{len(df)} bars report 0 volume (prices OK)"))

    gap = df.index.to_series().diff().dt.days
    big = gap[gap > 15]
    if len(big):
        issues.append(("gaps", f"{len(big)} gaps >15d, worst {int(big.max())}d "
                               f"at {big.idxmax().date()}"))

    if fix and changed:
        df.to_parquet(path, compression="snappy")

    return name, issues, changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ETF_DIR))
    ap.add_argument("--fix", action="store_true", help="adjust splits and drop bad prints")
    ap.add_argument("--quiet", action="store_true", help="only show repaired symbols")
    args = ap.parse_args()

    clean = flagged = repaired = 0
    counts: dict[str, int] = {}

    for path in sorted(Path(args.dir).glob("*.parquet")):
        name, issues, changed = scan(path, args.fix)
        for kind, _ in issues:
            counts[kind] = counts.get(kind, 0) + 1
        if not issues:
            clean += 1
            continue
        flagged += 1
        repaired += bool(changed)
        if args.quiet and not changed:
            continue
        print(f"\n{name}")
        for kind, detail in issues:
            print(f"   {kind:<16} {detail}")

    print(f"\n{clean} clean, {flagged} flagged"
          + (f", {repaired} repaired" if args.fix else ""))
    print("issue counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
