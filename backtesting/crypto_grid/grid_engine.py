import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Grid bot backtest engine — BTC on 1-minute bars
================================================
How the bot works (same as Binance / Pionex / Delta grid bots):
  * Levels are geometric: L_i = base * (1+g)^i  (g = grid step, e.g. 1%).
  * The bot covers a window of 2R cells around the price at start (or restart).
  * Each cell i (between L_i and L_{i+1}) gets the same money Q:
      buy  Q/L_i coins at L_i  ->  sell them at L_{i+1}   (profit = Q*g - fees)
  * "anchor" a = level of the last fill. Price falling to L_{a-1} buys a cell,
    rising to L_{a+1} sells one. Cells held = [a, top).
  * Limit orders fill exactly at their level (maker fee). Start/restart trades
    are market orders (taker fee).

Styles
  long    : spot-style grid. At start it buys the cells above price, so it is
            ~half long BTC and keeps more coins as price falls.
  neutral : futures grid. Starts flat; long below the start price, short above.
            Funding is paid/received on the open position (perp only).

Restart rules
  none : run the same window forever (price can leave it and the bot sits idle).
  exit : when price goes one step past either edge, close and re-centre the
         window on the current price (a "trailing" grid). Losses get realised.

Fills: each 1-min bar is walked O->L->H->C (green bar) or O->H->L->C (red bar).
Only points where price crosses a level matter, so the path is compressed to
those points (+ each day's last minute for the daily equity curve).
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parents[2]
BTC_DIR = ROOT / "data" / "CRYPTO_BTCUSDT"
FUNDING = ROOT / "data" / "CRYPTO_PERPS" / "BTCUSDT" / "funding.parquet"
DEFAULT_FUNDING_APR = 0.10   # used before Binance perp funding history starts (2020)


# --------------------------------------------------------------------------- data
def load_btc_1m(start: str, end: str) -> pd.DataFrame:
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    frames = []
    for y in range(s.year, e.year + 1):
        for p in sorted((BTC_DIR / str(y)).glob("ohlcv_1m_*.parquet")):
            frames.append(pd.read_parquet(p))
    df = pd.concat(frames, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"]).astype("datetime64[ns]")
    df = df[(df["datetime"] >= s) & (df["datetime"] < e + pd.Timedelta(days=1))]
    return df.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)


def build_path(df: pd.DataFrame):
    """Price path (4 points per bar), the day of each point, and a day-end flag."""
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    green = c >= o
    p2 = np.where(green, l, h)
    p3 = np.where(green, h, l)
    path = np.column_stack([o, p2, p3, c]).ravel()
    day = df["datetime"].dt.normalize().to_numpy()
    day4 = np.repeat(day, 4)
    last = np.zeros(len(path), bool)
    chg = np.r_[day4[1:] != day4[:-1], True]
    last[chg] = True
    return path, day4, last


def daily_funding(days: pd.DatetimeIndex) -> pd.Series:
    """Funding rate paid by longs per day (sum of the 8h rates)."""
    f = pd.read_parquet(FUNDING)
    f["day"] = pd.to_datetime(f["datetime"]).dt.normalize()
    s = f.groupby("day")["funding_rate"].sum()
    out = s.reindex(days)
    return out.fillna(DEFAULT_FUNDING_APR / 365)


# --------------------------------------------------------------------------- engine
@dataclass
class GridConfig:
    step: float = 0.01          # grid step g (1% = 0.01)
    half_range: float = 0.20    # window = price / (1+w) .. price * (1+w)
    style: str = "long"         # long | neutral
    restart: str = "exit"       # exit | none
    maker_fee: float = 0.001
    taker_fee: float = 0.001
    funding: bool = False       # perp: pay funding on the open position
    capital: float = 10_000.0


def run_grid(path, day4, last, cfg: GridConfig, funding_by_day: pd.Series | None = None,
             active=None):
    """active: optional bool array (one per path point). While False the bot is
    flat in cash; when it turns False the open grid is closed at market."""
    g = cfg.step
    lg = np.log1p(g)
    base = path[0]
    x = np.log(path / base) / lg                    # price in level units
    fl, ce = np.floor(x), np.ceil(x)
    keep = np.r_[True, (fl[1:] != fl[:-1]) | (ce[1:] != ce[:-1])] | last
    keep |= np.r_[True, last[:-1]]                   # first point of each day
    if active is not None:
        active = np.asarray(active, bool)
        keep |= np.r_[True, active[1:] != active[:-1]]
    idx = np.flatnonzero(keep)
    xs, ps, ds, lasts = x[idx], path[idx], day4[idx], last[idx]
    acts = active[idx] if active is not None else np.ones(len(idx), bool)

    R = max(1, int(np.ceil(np.log1p(cfg.half_range) / lg)))
    r = 1.0 / (1.0 + g)

    def S(a, b):  # sum_{i=a}^{b-1} (1+g)^-i  -> coins per unit Q is S/base
        if b <= a:
            return 0.0
        return (r ** a) * (1.0 - r ** (b - a)) / (1.0 - r)

    neutral = cfg.style == "neutral"
    fm, ft = cfg.maker_fee, cfg.taker_fee
    fund = funding_by_day if (cfg.funding and funding_by_day is not None) else None

    def start(equity, x0, p0, coins_now):
        """(Re)start the window at price p0. Returns state + cash after trades."""
        c = int(round(x0))
        lo, hi = c - R, c + R
        if neutral:
            Q = equity / R
            base_cells = S(c, hi)
            cash = equity - abs(coins_now) * p0 * ft      # close old position
            Q = cash / R
            return c, lo, hi, Q, base_cells, cash
        Q = equity / (2 * R)
        target = Q / base * S(c, hi)                     # coins for cells above price
        trade = abs(target - coins_now) * p0
        cash = equity - target * p0 - trade * ft
        return c, lo, hi, Q, 0.0, cash

    eq0 = cfg.capital
    on = bool(acts[0])
    if on:
        a, lo, hi, Q, base_cells, cash = start(eq0, xs[0], ps[0], 0.0)
    else:
        a = lo = hi = 0; Q = base_cells = 0.0; cash = eq0
    restarts = 0
    round_trips = 0
    fees = 0.0
    days_on = 0
    paused_day = None
    out_days, out_eq, out_px, out_pos = [], [], [], []

    for j in range(len(xs)):
        xj = xs[j]
        # --- regime switch
        if on and not acts[j]:
            coins = Q / base * (S(a, hi) - base_cells)
            cash += coins * ps[j] - abs(coins) * ps[j] * cfg.taker_fee
            on = False
        elif not on and acts[j] and ds[j] != paused_day:
            a, lo, hi, Q, base_cells, cash = start(cash, xj, ps[j], 0.0)
            on = True
        if not on:
            if lasts[j]:
                out_days.append(ds[j]); out_eq.append(cash); out_px.append(ps[j]); out_pos.append(0.0)
            continue
        # --- fills inside the window
        if xj <= a - 1:
            na = max(int(np.ceil(xj - 1e-9)), lo)
            if na < a:
                n = a - na
                cash -= Q * n * (1 + fm)
                fees += Q * n * fm
                a = na
        elif xj >= a + 1:
            na = min(int(np.floor(xj + 1e-9)), hi)
            if na > a:
                n = na - a
                cash += Q * (1 + g) * n * (1 - fm)
                fees += Q * (1 + g) * n * fm
                round_trips += n
                a = na
        coins = Q / base * (S(a, hi) - base_cells)
        # --- restart when price is a full step outside the window
        if cfg.restart in ("exit", "pause") and (xj >= hi + 1 or xj <= lo - 1):
            restarts += 1
            if cfg.restart == "pause":      # breakout: close, wait for the next day
                cash += coins * ps[j] - abs(coins) * ps[j] * cfg.taker_fee
                on = False; paused_day = ds[j]; coins = 0.0
                if lasts[j]:
                    out_days.append(ds[j]); out_eq.append(cash); out_px.append(ps[j]); out_pos.append(0.0)
                continue
            eq = cash + coins * ps[j]
            a, lo, hi, Q, base_cells, cash = start(eq, xj, ps[j], coins)
            coins = Q / base * (S(a, hi) - base_cells)
        if lasts[j]:
            days_on += 1
            p = ps[j]
            if fund is not None:
                cash -= coins * p * fund.get(ds[j], 0.0)   # longs pay, shorts receive
            out_days.append(ds[j]); out_eq.append(cash + coins * p)
            out_px.append(p); out_pos.append(coins * p)

    daily = pd.DataFrame({"equity": out_eq, "price": out_px, "position": out_pos},
                         index=pd.DatetimeIndex(out_days, name="day"))
    stats = {"restarts": restarts, "round_trips": round_trips, "fees": fees,
             "levels": 2 * R, "events": len(xs), "days_on": days_on}
    return daily, stats


def metrics(eq: pd.Series, capital: float) -> dict:
    eq = eq.dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    ret = eq.pct_change().dropna()
    dd = eq / eq.cummax() - 1
    final = eq.iloc[-1]
    cagr = (final / capital) ** (1 / yrs) - 1 if final > 0 else -1.0
    sharpe = ret.mean() / ret.std() * np.sqrt(365) if ret.std() > 0 else 0.0
    return {"final": final, "total_%": (final / capital - 1) * 100, "cagr_%": cagr * 100,
            "max_dd_%": dd.min() * 100, "sharpe": sharpe}
