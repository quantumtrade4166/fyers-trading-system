import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""Truncate-and-compare: every config of every book-2 strategy must give the
same positions for bars that exist in both the full and the cut series.
    .venv\Scripts\python.exe -W ignore -m backtesting.book_strategies.schwager.test_lookahead
"""
import numpy as np
from backtesting.book_strategies.schwager import data as D, strategies as S

fails = 0; n = 0
for mk in ("NIFTY", "EURUSD"):
    df = D.load(mk).iloc[:2600]
    cut = 2200
    full, part = S.prep(df), S.prep(df.iloc[:cut])
    for sid, name, fn, cfgs in S.BOOK:
        for cfg in cfgs:
            a = fn(full, cfg)[:cut]; b = fn(part, cfg)
            n += 1
            # seasonal is monthly: the last partial month may differ by design
            if sid == "2.50":
                m = part["month"]; keep = ~((part["year"] == part["year"][-1]) & (m == m[-1]))
                keep[-25:] = False
                a, b = a[keep], b[keep]
            if not np.array_equal(np.nan_to_num(a), np.nan_to_num(b)):
                fails += 1
                bad = np.where(np.nan_to_num(a) != np.nan_to_num(b))[0]
                print(f"FAIL {mk} {sid} {cfg}: first diff at {bad[0]} of {cut} ({len(bad)} bars)")
print(f"{n - fails}/{n} configs pass")
sys.exit(1 if fails else 0)
