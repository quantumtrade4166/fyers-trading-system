import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Portfolio simulator — turns a pile of per-symbol trades into ONE account
with real capital limits.

Each symbol's trades come from the harness sized for the market's base
capital (e.g. Rs10L, 1% risk). This replays them in entry order:
  • risk compounds: a trade is scaled by (equity at entry / base capital)
  • cash only, no leverage: a trade is SKIPPED if its notional doesn't fit
    in free cash (equity − notional of positions still open)
  • cash from a closed trade is free from the NEXT day
  • several entries on the same day are taken in a fixed random order
    (seeded), so no hidden preference for any symbol

Equity is booked on exit (realised). Holding periods are short (days), so
the drawdown is close to, but slightly kinder than, mark-to-market.
"""

import numpy as np
import pandas as pd


def simulate(trades: pd.DataFrame, capital: float, seed: int = 7,
             calendar: str = "business", adv_frac: float = None) -> tuple[pd.DataFrame, pd.Series]:
    if trades.empty:
        return trades.assign(taken=False), pd.Series(dtype=float)
    t = trades.copy()
    t["entry_time"] = pd.to_datetime(t["entry_time"])
    t["exit_time"] = pd.to_datetime(t["exit_time"])
    rng = np.random.default_rng(seed)
    t["_r"] = rng.random(len(t))
    t = t.sort_values(["entry_time", "_r"]).reset_index(drop=True)

    entry_d = t["entry_time"].dt.normalize().to_numpy()
    exit_d = t["exit_time"].dt.normalize().to_numpy()
    notional0 = (t["entry_price"] * t["units"]).to_numpy(float)
    pnl0 = t["pnl"].to_numpy(float)
    # capacity: a position may not exceed adv_frac x the stock's average daily
    # traded value (adv20) — a large account can't buy Rs1 cr of a small cap
    adv = (t["adv20"].to_numpy(float) if adv_frac and "adv20" in t else None)

    equity = capital
    open_pos: list = []                 # (exit_day, notional, pnl)
    taken = np.zeros(len(t), bool)
    scale = np.zeros(len(t))
    for i in range(len(t)):
        keep = []
        for ex, no, pn in open_pos:     # release trades that closed before today
            if ex < entry_d[i]:
                equity += pn
            else:
                keep.append((ex, no, pn))
        open_pos = keep
        k = max(equity, 0.0) / capital
        if adv is not None and notional0[i] > 0:
            cap_k = adv_frac * (adv[i] if adv[i] == adv[i] else 0.0) / notional0[i]
            k = min(k, cap_k)
        need = notional0[i] * k
        free = equity - sum(no for _, no, _ in open_pos)
        # 0.01% tolerance: the harness rounds units to 4 decimals, so a trade
        # sized at exactly 100% can land a hair over it on small accounts.
        if k > 0 and need <= free * (1 + 1e-4):
            taken[i], scale[i] = True, k
            open_pos.append((exit_d[i], need, pnl0[i] * k))

    t["taken"], t["scale"] = taken, scale
    t["pnl_acct"] = t["pnl"] * t["scale"]
    booked = t[t["taken"]].groupby(t["exit_time"].dt.normalize())["pnl_acct"].sum()
    rng_fn = pd.date_range if calendar == "calendar" else pd.bdate_range   # crypto trades 7 days
    days = rng_fn(t["entry_time"].min().normalize(), t["exit_time"].max().normalize())
    eq = capital + booked.reindex(days.union(booked.index), fill_value=0.0).cumsum()
    return t.drop(columns="_r"), eq


def stats(eq: pd.Series, t: pd.DataFrame, start=None, end=None,
          calendar: str = "business") -> dict:
    """CAGR / drawdown / Sharpe of the equity curve inside [start, end)."""
    e = eq.copy()
    if start is not None:
        e = e[e.index >= pd.Timestamp(start)]
    if end is not None:
        e = e[e.index < pd.Timestamp(end)]
    if len(e) < 2:
        return {"trades": 0}
    tt = t[t["taken"]]
    tt = tt[(tt["exit_time"] >= e.index[0]) & (tt["exit_time"] <= e.index[-1])]
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    ret = e.pct_change().dropna()
    wins, losses = tt.loc[tt["pnl_acct"] > 0, "pnl_acct"].sum(), -tt.loc[tt["pnl_acct"] < 0, "pnl_acct"].sum()
    return {
        "trades":     len(tt),
        "win_pct":    round((tt["pnl_acct"] > 0).mean() * 100, 1) if len(tt) else 0.0,
        "pf":         round(wins / losses, 2) if losses > 0 else np.inf,
        "avgR_net":   round(tt["R"].mean(), 3) if len(tt) else 0.0,
        "avgR_gross": round(tt["R_gross"].mean(), 3) if len(tt) else 0.0,
        "cagr_pct":   round(((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100, 2) if yrs > 0 and e.iloc[-1] > 0 else -100.0,
        "maxdd_pct":  round(((e - e.cummax()) / e.cummax()).min() * 100, 1),
        "sharpe":     round(ret.mean() / ret.std() * np.sqrt(252), 2) if ret.std() > 0 else 0.0,
        "end_equity": round(e.iloc[-1]),
    }
