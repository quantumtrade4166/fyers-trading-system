import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

"""
Volume spike + EMA 9/21 cross — the owner's rule (2026-10-20), 1-hour candles.

LONG  : a 1h candle's volume > MULT x the average volume of the previous N days of
        1h candles (N = 3 or 4 -> 72 / 96 candles, the spike candle itself excluded),
        the spike candle is green, and EMA9 crosses ABOVE EMA21 on that candle or
        within `window` candles of it (either order). Enter at the next candle's
        open. Stop-loss = the spike candle's LOW.
SHORT : mirror — red spike candle, EMA9 crosses below EMA21, stop = spike HIGH.

Exits tested (stop always live): 'cross' opposite EMA cross (at that candle's
close), 'R2' / 'R3' target at 2R / 3R, 'h24' close after 24 candles.
Stop and target touched in the same candle -> counted as the STOP (pessimistic).
A gap through the stop fills at the open, not at the stop.

Sizing: risk RISK of equity per trade at the stop, notional capped at MAX_LEV x equity.
Costs : Delta futures taker 0.05% + 18% GST = 0.059% per side + SLIP per side;
        Binance funding on the position while held (long pays a positive rate).
Baseline: use_volume=False -> the same EMA-cross entries with no volume condition
          (stop = signal candle low/high), to see if the volume spike adds anything.

Run: .venv\\Scripts\\python.exe -m backtesting.crypto_volume.volspike_ema
"""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).parents[2]
PERPS = ROOT / "data" / "CRYPTO_PERPS"
OUT = Path(__file__).parent / "results"

FEE = 0.0005 * 1.18
SLIP = 0.0001
RISK = 0.01
MAX_LEV = 3.0
CAP = 10_000.0


def load(perp: str = "BTCUSDT") -> pd.DataFrame:
    k = pd.read_parquet(PERPS / perp / "klines_1h.parquet").set_index("datetime").sort_index()
    k = k[k["volume"] > 0]
    f = pd.read_parquet(PERPS / perp / "funding.parquet").set_index("datetime")["funding_rate"]
    # a settlement at hh:00 is paid by whoever holds through it -> charge it to the bar ending there
    f.index = f.index.floor("h") - pd.Timedelta(hours=1)
    k["funding"] = f.groupby(level=0).sum().reindex(k.index).fillna(0.0)
    return k


def daily_trend(df: pd.DataFrame, ema_days: int) -> pd.Series:
    """Hourly flag: last 1h close above the daily EMA of the PREVIOUS completed day
    (UTC days). No look-ahead: today's daily close is not known until midnight."""
    d = df["close"].resample("1D").last().dropna()
    ema = d.ewm(span=ema_days, adjust=False, min_periods=ema_days).mean().shift(1)
    ema_h = ema.reindex(df.index.floor("D")).to_numpy()
    return pd.Series(df["close"].to_numpy() > ema_h, index=df.index) & ~np.isnan(ema_h)


def signals(df: pd.DataFrame, days: int = 3, mult: float = 5.0, window: int = 0,
            use_volume: bool = True, candle_dir: bool = True,
            long_only: bool = False, trend_days: int | None = None) -> pd.DataFrame:
    sig = _signals(df, days, mult, window, use_volume, candle_dir)
    if long_only:
        sig["short"] = False
    if trend_days:
        up = daily_trend(df, trend_days)
        sig["long"] &= up
        sig["short"] &= ~up                   # shorts (if any) only below the trend line
    return sig


def _signals(df: pd.DataFrame, days: int = 3, mult: float = 5.0, window: int = 0,
             use_volume: bool = True, candle_dir: bool = True) -> pd.DataFrame:
    c = df["close"]
    e9, e21 = c.ewm(span=9, adjust=False).mean(), c.ewm(span=21, adjust=False).mean()
    up = (e9 > e21) & (e9.shift() <= e21.shift())
    dn = (e9 < e21) & (e9.shift() >= e21.shift())
    n = days * 24
    avg = df["volume"].shift(1).rolling(n, min_periods=n).mean()
    spike = df["volume"] > mult * avg
    green, red = df["close"] > df["open"], df["close"] < df["open"]
    sig = pd.DataFrame(index=df.index)
    if not use_volume:
        sig["long"], sig["short"] = up, dn
        sig["stop_long"], sig["stop_short"] = df["low"], df["high"]
        return sig
    spike_l = spike & (green if candle_dir else True)
    spike_s = spike & (red if candle_dir else True)
    # remember the most recent qualifying spike candle's low/high for `window` candles
    lo = df["low"].where(spike_l)
    hi = df["high"].where(spike_s)
    lo_recent = lo.ffill(limit=window) if window else lo
    hi_recent = hi.ffill(limit=window) if window else hi
    spike_l_recent = spike_l.astype(float).rolling(window + 1, min_periods=1).max() > 0
    spike_s_recent = spike_s.astype(float).rolling(window + 1, min_periods=1).max() > 0
    up_recent = up.astype(float).rolling(window + 1, min_periods=1).max() > 0
    dn_recent = dn.astype(float).rolling(window + 1, min_periods=1).max() > 0
    # trigger on the candle where the SECOND condition arrives
    long_ = (up & spike_l_recent) | (spike_l & up_recent)
    short_ = (dn & spike_s_recent) | (spike_s & dn_recent)
    sig["long"], sig["short"] = long_, short_
    sig["stop_long"], sig["stop_short"] = lo_recent, hi_recent
    sig["cross_up"], sig["cross_dn"] = up, dn
    return sig


@dataclass
class Trade:
    side: int
    entry_t: pd.Timestamp
    entry: float
    stop: float
    qty: float
    target: float | None
    exit_t: pd.Timestamp | None = None
    exit: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    R: float = 0.0


def backtest(df: pd.DataFrame, sig: pd.DataFrame, exit_rule: str = "cross") -> tuple[list[Trade], pd.Series]:
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    fund = df["funding"].to_numpy()
    t = df.index
    L, S = sig["long"].to_numpy(), sig["short"].to_numpy()
    SL, SS = sig["stop_long"].to_numpy(), sig["stop_short"].to_numpy()
    e9 = df["close"].ewm(span=9, adjust=False).mean().to_numpy()
    e21 = df["close"].ewm(span=21, adjust=False).mean().to_numpy()
    eq = CAP
    curve = np.full(len(t), np.nan)
    trades: list[Trade] = []
    pos: Trade | None = None
    pending = None                                    # (side, stop) to enter at next open
    held = 0
    for i in range(len(t)):
        # ── entry at this candle's open ──
        if pending is not None and pos is None:
            side, stop = pending
            px = o[i] * (1 + SLIP * side)
            risk_px = (px - stop) * side
            if risk_px > 0:
                qty = min(RISK * eq / risk_px, MAX_LEV * eq / px)
                tgt = None
                if exit_rule in ("R2", "R3"):
                    tgt = px + side * risk_px * (2 if exit_rule == "R2" else 3)
                fee = qty * px * FEE
                eq -= fee
                pos = Trade(side, t[i], px, stop, qty, tgt, fees=fee)
                held = 0
        pending = None
        # ── manage the open trade inside this candle ──
        if pos is not None:
            s = pos.side
            exit_px, why = None, ""
            if s == 1 and l[i] <= pos.stop:
                exit_px, why = min(o[i], pos.stop), "stop"
            elif s == -1 and h[i] >= pos.stop:
                exit_px, why = max(o[i], pos.stop), "stop"
            elif pos.target is not None and ((s == 1 and h[i] >= pos.target) or (s == -1 and l[i] <= pos.target)):
                exit_px, why = (max(o[i], pos.target) if s == 1 else min(o[i], pos.target)), "target"
            else:
                held += 1
                opp = (e9[i] < e21[i] and e9[i - 1] >= e21[i - 1]) if s == 1 else \
                      (e9[i] > e21[i] and e9[i - 1] <= e21[i - 1])
                if exit_rule == "cross" and opp:
                    exit_px, why = c[i], "cross"
                elif exit_rule == "h24" and held >= 24:
                    exit_px, why = c[i], "time"
            # funding for holding through this candle (only if still held at its end)
            if exit_px is None:
                f = pos.qty * c[i] * fund[i] * s
                pos.funding += f
                eq -= f
            else:
                exit_px *= (1 - SLIP * s)
                fee = pos.qty * exit_px * FEE
                gross = (exit_px - pos.entry) * pos.qty * s
                eq += gross - fee
                pos.fees += fee
                pos.exit_t, pos.exit, pos.reason = t[i], exit_px, why
                pos.pnl = gross - pos.fees - pos.funding
                pos.R = pos.pnl / (abs(pos.entry - pos.stop) * pos.qty)
                trades.append(pos)
                pos = None
        # ── new signal at this candle's close (one trade at a time) ──
        if pos is None:
            if L[i] and not np.isnan(SL[i]):
                pending = (1, SL[i])
            elif S[i] and not np.isnan(SS[i]):
                pending = (-1, SS[i])
        mtm = eq + ((c[i] - pos.entry) * pos.qty * pos.side if pos else 0.0)
        curve[i] = mtm
    return trades, pd.Series(curve, index=t)


def report(trades: list[Trade], curve: pd.Series) -> dict:
    d = curve.resample("1D").last().dropna()
    r = d.pct_change().dropna()
    yrs = (d.index[-1] - d.index[0]).days / 365
    pnl = np.array([x.pnl for x in trades]) if trades else np.array([0.0])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    m = d.resample("ME").last().pct_change().dropna()
    return {
        "trades": len(trades), "per_year": len(trades) / yrs if yrs else 0,
        "win%": (pnl > 0).mean() if trades else 0,
        "avgR": np.mean([x.R for x in trades]) if trades else 0,
        "PF": wins.sum() / -losses.sum() if losses.sum() < 0 else np.nan,
        "end$": d.iloc[-1], "CAGR": (d.iloc[-1] / CAP) ** (1 / yrs) - 1 if yrs else 0,
        "maxDD": (d / d.cummax() - 1).min(),
        "Sharpe": r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else np.nan,
        "months_up": (m > 0).mean(), "fees$": sum(x.fees for x in trades),
        "funding$": sum(x.funding for x in trades),
        "long$": sum(x.pnl for x in trades if x.side == 1),
        "short$": sum(x.pnl for x in trades if x.side == -1),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--perp", default="BTCUSDT")
    args = ap.parse_args()
    df = load(args.perp)
    print(f"{args.perp} 1h: {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}, {len(df):,} candles")
    rows = []
    hdr = (f"{'variant':<34}{'trades':>7}{'/yr':>6}{'win%':>6}{'avgR':>7}{'PF':>6}{'end $':>10}"
           f"{'CAGR':>8}{'maxDD':>8}{'Sharpe':>7}{'long $':>9}{'short $':>9}")
    print(hdr)
    for use_vol in (True, False):
        for days in ((3, 4) if use_vol else (3,)):
            for window in ((0, 3, 6) if use_vol else (0,)):
                for ex in ("cross", "R2", "R3", "h24"):
                    sig = signals(df, days=days, mult=5.0, window=window, use_volume=use_vol)
                    tr, cv = backtest(df, sig, ex)
                    m = report(tr, cv)
                    name = (f"vol>5x {days}d avg, win {window}, exit {ex}" if use_vol
                            else f"NO volume, EMA cross only, exit {ex}")
                    rows.append({"variant": name, "use_volume": use_vol, "days": days, "window": window,
                                 "exit": ex, **m})
                    print(f"{name:<34}{m['trades']:>7}{m['per_year']:>6.0f}{m['win%']:>6.0%}{m['avgR']:>+7.2f}"
                          f"{m['PF']:>6.2f}{m['end$']:>10,.0f}{m['CAGR']:>+8.1%}{m['maxDD']:>8.1%}"
                          f"{m['Sharpe']:>7.2f}{m['long$']:>+9,.0f}{m['short$']:>+9,.0f}", flush=True)
    OUT.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / f"volspike_ema_{args.perp}.csv", index=False)


if __name__ == "__main__":
    main()
