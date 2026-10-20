"""
Taker Flow L/S — the signal (same rules as the backtest,
backtesting/crypto_volume/sim_delta_charges.py).

1. Binance USDT-M perps that are TRADING; daily klines (COMPLETED bars only).
2. Universe: top N by average quote volume over adv_days, listed >= warmup_days.
3. Keep coins Delta India lists as a live perp with 24h turnover >= min_delta_turnover.
4. Score = taker-buy volume / total volume over lookback_days.
5. Long the top q, short the bottom q; inverse-vol (vol_days of daily close
   returns) inside each book; each book sums to 1.

Every network failure raises — a half-fetched universe must never be traded.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np

BINANCE = "https://fapi.binance.com"
DELTA = "https://api.india.delta.exchange"
_UA = "taker-flow-paper/1.0"


class FeedError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None, retries: int = 3, timeout: int = 20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 418:                          # IP banned: retrying only extends it
                raise FeedError(f"GET {url}: Binance IP ban (418) — back off") from e
            # 429 = over the rate budget: Binance says wait (Retry-After), usually ~60 s
            wait = float(e.headers.get("Retry-After") or 60) if e.code == 429 else 1.5 * (attempt + 1)
            time.sleep(min(wait, 120))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise FeedError(f"GET {url} failed: {last}")


# ── Binance ──────────────────────────────────────────────────────────────
def binance_perps() -> list[dict]:
    syms = _get(f"{BINANCE}/fapi/v1/exchangeInfo")["symbols"]
    return [s for s in syms if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def binance_daily(symbol: str, limit: int = 60) -> list[dict]:
    """Completed daily bars only (the bar still forming is dropped)."""
    rows = _get(f"{BINANCE}/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
    now_ms = time.time() * 1000
    out = []
    for r in rows:
        if r[6] >= now_ms:                    # close_time in the future -> still forming
            continue
        out.append({"t": int(r[0]), "close": float(r[4]), "volume": float(r[5]),
                    "quote_volume": float(r[7]), "taker_buy": float(r[9])})
    return out


# ── Delta ────────────────────────────────────────────────────────────────
def delta_perps() -> dict[str, dict]:
    """Live, operational Delta perps keyed by underlying, with ticker fields merged."""
    prods, after = [], None
    while True:
        p = {"contract_types": "perpetual_futures", "states": "live", "page_size": 500}
        if after:
            p["after"] = after
        d = _get(f"{DELTA}/v2/products", p)
        prods += d.get("result") or []
        after = (d.get("meta") or {}).get("after")
        if not after or not d.get("result"):
            break
    tick = {t["symbol"]: t for t in
            _get(f"{DELTA}/v2/tickers", {"contract_types": "perpetual_futures"})["result"]}
    out = {}
    for p in prods:
        t = tick.get(p["symbol"])
        if not t or t.get("product_trading_status") not in (None, "operational"):
            continue
        if p.get("quoting_asset", {}).get("symbol") != "USD":
            continue
        u = p["underlying_asset"]["symbol"]
        q = t.get("quotes") or {}
        out[u] = {"symbol": p["symbol"], "product_id": p["id"],
                  "best_bid": float(q.get("best_bid") or 0), "best_ask": float(q.get("best_ask") or 0),
                  "bid_size": float(q.get("bid_size") or 0), "ask_size": float(q.get("ask_size") or 0),
                  "contract_value": float(p["contract_value"]),
                  "taker_fee": float(p["taker_commission_rate"]),
                  "maker_fee": float(p["maker_commission_rate"]),
                  "mark": float(t["mark_price"]),
                  "funding_rate_8h_pct": float(t.get("funding_rate") or 0.0),
                  "turnover_usd": float(t.get("turnover_usd") or 0.0)}
    return out


# not directional crypto risk — same exclusions as the backtest universe
EXCLUDE = {"XAUT", "PAXG", "XAU", "XAG", "SLVON", "USDC", "USDE", "FDUSD", "TUSD", "BUSD",
           "USDP", "USD1", "EUR", "BTCDOM", "DEFI", "FOOTBALL", "BLUEBIRD"}


def map_binance_to_delta(binance_symbol: str, delta: dict) -> str | None:
    base = binance_symbol[:-4]                         # strip USDT
    if base in EXCLUDE:
        return None
    for prefix in ("1000000", "1000"):
        if base.startswith(prefix) and base[len(prefix):] in delta:
            return base[len(prefix):]
    return base if base in delta else None


# ── the ranking ──────────────────────────────────────────────────────────
def build_signal(cfg: dict, log=print) -> dict:
    """Return {'asof', 'universe', 'eligible', 'longs', 'shorts', 'weights', 'table'}.
    weights: {delta_underlying: signed weight}, each book summing to +1 / -1."""
    delta = delta_perps()
    perps = binance_perps()
    # one call for 24h volume; only the busiest prefilter_n get daily history
    # (the 30-day top 100 always sits well inside the 24h top 250)
    vol24 = {t["symbol"]: float(t["quoteVolume"]) for t in _get(f"{BINANCE}/fapi/v1/ticker/24hr")}
    perps = sorted(perps, key=lambda s: vol24.get(s["symbol"], 0.0), reverse=True)
    perps = perps[:cfg.get("prefilter_n", 250)]
    now = datetime.now(timezone.utc)
    need = max(cfg["adv_days"], cfg["lookback_days"], cfg["vol_days"]) + 2
    rows = []
    for i, s in enumerate(perps):
        age_days = (now.timestamp() * 1000 - s["onboardDate"]) / 86_400_000
        if age_days < cfg["warmup_days"]:
            continue
        bars = binance_daily(s["symbol"], limit=need + 1)
        if len(bars) < max(cfg["adv_days"], cfg["vol_days"]) // 2:
            continue
        adv = np.mean([b["quote_volume"] for b in bars[-cfg["adv_days"]:]])
        rows.append((s["symbol"], adv, bars))
        if i % 50 == 49:
            time.sleep(0.5)                            # stay far below the rate limit
    rows.sort(key=lambda r: r[1], reverse=True)
    universe = rows[:cfg["universe_top_n"]]

    table = []
    for sym, adv, bars in universe:
        u = map_binance_to_delta(sym, delta)
        lb = bars[-cfg["lookback_days"]:]
        if len(lb) < cfg["lookback_days"]:
            continue
        ratio = sum(b["taker_buy"] for b in lb) / max(sum(b["volume"] for b in lb), 1e-12)
        closes = np.array([b["close"] for b in bars[-(cfg["vol_days"] + 1):]])
        rets = closes[1:] / closes[:-1] - 1
        vol = float(np.std(rets, ddof=1)) if len(rets) >= 10 else float("nan")
        ok = (u is not None and delta[u]["turnover_usd"] >= cfg["min_delta_turnover_usd"]
              and np.isfinite(vol) and vol > 0)
        table.append({"binance": sym, "delta": u, "adv_usd": adv, "taker_ratio": ratio,
                      "vol_daily": vol, "eligible": bool(ok),
                      "delta_turnover_usd": delta[u]["turnover_usd"] if u else None})

    elig = sorted([r for r in table if r["eligible"]], key=lambda r: r["taker_ratio"])
    weights, longs, shorts = {}, [], []
    if len(elig) >= cfg["min_coins"]:
        k = max(1, int(len(elig) * cfg["bucket_q"]))
        shorts, longs = elig[:k], elig[-k:]
        for book, sign in ((longs, 1.0), (shorts, -1.0)):
            inv = np.array([1 / r["vol_daily"] for r in book])
            for r, w in zip(book, inv / inv.sum()):
                weights[r["delta"]] = sign * float(w)
    else:
        log(f"only {len(elig)} eligible coins (< {cfg['min_coins']}) — book stays flat")
    for r in table:
        r["side"] = ("long" if r in longs else "short" if r in shorts else "")
        r["weight"] = weights.get(r["delta"], 0.0) if r["side"] else 0.0
    return {"asof": now.isoformat(timespec="seconds"), "universe": len(universe),
            "eligible": len(elig), "longs": [r["delta"] for r in longs],
            "shorts": [r["delta"] for r in shorts], "weights": weights,
            "table": table, "delta": delta}
