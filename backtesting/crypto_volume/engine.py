"""
Portfolio engine for the crypto volume backtests.

Timing : signal at bar t close -> held from bar t+1 open.  Bar return is
         open-to-open, so no fill ever uses a price the signal saw.
Sizing : time-series strategies — each coin gets 1 / (vol x N_active), fixed
         at entry (no daily re-sizing churn).  Cross-sectional — long book +1,
         short book -1, inverse-vol within each book, held between rebalances.
Costs  : cost_bps per side on every change in weight (fee + slippage), plus
         funding on every bar held (long pays +rate, short receives).
Universe: a coin joins WARMUP_DAYS after its first bar (listing weeks are wild).
"""

import numpy as np
import pandas as pd

from . import data as D

WARMUP_DAYS = 30
IS_END = pd.Timestamp("2023-12-31 23:59")


class Panel:
    """All coins for one timeframe, aligned on a common UTC bar index."""

    def __init__(self, tf: str, perps: list[str] | None = None,
                 universe: str = "top100", pit_n: int = 100, pit_days: int = 30):
        """universe='top100': today's CoinGecko top 100 (survivorship-biased).
        universe='pit': every perp ever listed (incl. delisted); on each bar the
        tradable set is the top pit_n by trailing pit_days quote volume, known
        at that bar's close — no hindsight."""
        self.tf = tf
        self.universe = universe
        self.bpy = D.BARS_PER_YEAR[tf]
        self.bpd = self.bpy // 365
        perps = perps or (D.all_coins() if universe == "pit" else D.available())
        self.dfs = {p: D.load(p, tf) for p in perps}
        idx = sorted(set().union(*[d.index for d in self.dfs.values()]))
        self.index = pd.DatetimeIndex(idx)
        col = lambda c: pd.DataFrame({p: d[c] for p, d in self.dfs.items()}).reindex(self.index)
        self.open, self.close, self.volume = col("open"), col("close"), col("volume")
        self.taker_buy = col("taker_buy_volume")
        self.funding = col("funding").fillna(0.0)
        # open-to-open return of bar t (held from open t to open t+1)
        self.ret = (self.open.shift(-1) / self.open - 1)
        first = self.close.apply(lambda s: s.first_valid_index())
        age_ok = pd.DataFrame({p: self.index >= first[p] + pd.Timedelta(days=WARMUP_DAYS)
                               for p in self.close.columns}, index=self.index)
        self.active = age_ok & self.close.notna() & self.ret.notna()
        if universe == "pit":
            qv = col("quote_volume")
            n = pit_days * self.bpd
            adv = qv.rolling(n, min_periods=n // 2).mean().where(age_ok)
            rank = adv.rank(axis=1, ascending=False)
            self.member = rank <= pit_n
            self.active &= self.member
        cc = self.close.pct_change(fill_method=None)
        self.vol = cc.rolling(30 * self.bpd, min_periods=10 * self.bpd).std()   # per-bar sigma


# ── weights ──────────────────────────────────────────────────────────────
def ts_weights(pnl: Panel, positions: pd.DataFrame) -> pd.DataFrame:
    """Signed positions (-1/0/1) -> weights sized at entry, held until change."""
    n_act = pnl.active.sum(axis=1).replace(0, np.nan)
    raw = (1.0 / pnl.vol).div(n_act, axis=0)
    # no position until the coin's vol is known — else an entry made before
    # that would stay unsized for the whole hold
    pos = positions.reindex(pnl.index).where(pnl.active & raw.notna()).fillna(0.0)
    change = pos.ne(pos.shift()) | pos.eq(0)
    size = raw.where(change).ffill()
    return (pos * size).fillna(0.0)


def xs_weights(pnl: Panel, score: pd.DataFrame, rebal: int, q: float = 0.2,
               eligible_long: pd.DataFrame | None = None,
               eligible_short: pd.DataFrame | None = None, min_coins: int = 10) -> pd.DataFrame:
    """Top-q long / bottom-q short by score, inverse-vol inside each book."""
    w = pd.DataFrame(np.nan, index=pnl.index, columns=pnl.close.columns)
    s_all = score.where(pnl.active)
    inv = 1.0 / pnl.vol
    for i in range(0, len(pnl.index), rebal):
        s = s_all.iloc[i].dropna()
        row = pd.Series(0.0, index=w.columns)
        if len(s) >= min_coins:
            k = max(1, int(len(s) * q))
            ranked = s.sort_values()
            longs, shorts = ranked.index[-k:], ranked.index[:k]
            if eligible_long is not None:
                el = eligible_long.iloc[i]
                longs = [c for c in ranked.index[::-1] if el.get(c, False)][:k]
            if eligible_short is not None:
                es = eligible_short.iloc[i]
                shorts = [c for c in ranked.index if es.get(c, False)][:k]
            for book, sign in ((longs, 1.0), (shorts, -1.0)):
                if len(book):
                    iv = inv.iloc[i][list(book)].fillna(0)
                    if iv.sum() > 0:
                        row[list(book)] = sign * iv / iv.sum()
        w.iloc[i] = row
    return w.ffill().where(pnl.active).fillna(0.0)


# ── P&L + metrics ────────────────────────────────────────────────────────
def run(pnl: Panel, weights: pd.DataFrame, cost_bps: float = 8.0) -> pd.DataFrame:
    """Per-bar portfolio P&L. weights[t] is decided at close t, held during bar t+1."""
    held = weights.shift().fillna(0.0)
    gross = (held * pnl.ret.fillna(0.0)).sum(axis=1)
    cost = held.diff().abs().fillna(held.abs()).sum(axis=1) * cost_bps / 1e4
    fund = (held * pnl.funding).sum(axis=1)
    long_ = (held.clip(lower=0) * pnl.ret.fillna(0.0)).sum(axis=1)
    return pd.DataFrame({"gross": gross, "cost": cost, "funding": fund,
                         "net": gross - cost - fund, "long_gross": long_,
                         "exposure": held.abs().sum(axis=1),
                         "turnover": held.diff().abs().sum(axis=1)})


def _sharpe(d: pd.Series) -> float:
    d = d.dropna()
    return float(d.mean() / d.std() * np.sqrt(365)) if len(d) > 30 and d.std() > 0 else np.nan


def metrics(res: pd.DataFrame, target_vol: float = 0.20) -> dict:
    daily = res.resample("1D").sum()
    daily = daily.loc[daily["exposure"].gt(0).idxmax():]          # from first trade
    net = daily["net"]
    scale = target_vol / (net.std() * np.sqrt(365)) if net.std() > 0 else 0
    eq = (1 + net * scale).cumprod()
    yrs = len(net) / 365
    dd = eq / eq.cummax() - 1
    gross_tot = daily["gross"].sum()
    return {
        "sharpe": _sharpe(net),
        "sharpe_is": _sharpe(net[net.index <= IS_END]),
        "sharpe_oos": _sharpe(net[net.index > IS_END]),
        "sharpe_2025_26": _sharpe(net[net.index >= "2025-01-01"]),
        "cagr_at20vol": float(eq.iloc[-1] ** (1 / yrs) - 1) if yrs > 0 else np.nan,
        "maxdd_at20vol": float(dd.min()),
        "cost_share": float(daily["cost"].sum() / gross_tot) if gross_tot > 0 else np.nan,
        "funding_share": float(daily["funding"].sum() / gross_tot) if gross_tot > 0 else np.nan,
        "long_share": float(daily["long_gross"].sum() / gross_tot) if gross_tot > 0 else np.nan,
        "turnover_yr": float(daily["turnover"].sum() / yrs / max(daily["exposure"].mean(), 1e-12)),
        "start": str(net.index[0].date()),
    }
