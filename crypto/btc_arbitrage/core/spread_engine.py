"""
core/spread_engine.py — executable spreads from two tops of book.
=================================================================

Sign convention (everything in USD per 1 BTC):

    POSITIVE  Binance is richer than Delta  -> BUY DELTA,   SELL BINANCE
    NEGATIVE  Delta is richer than Binance  -> BUY BINANCE, SELL DELTA

The two executable directions:

    s1 = Binance_bid - Delta_ask     BUY Delta at its ask, SELL Binance at its bid
    s2 = Delta_bid  - Binance_ask    BUY Binance at its ask, SELL Delta at its bid

At most ONE of them can be positive (both positive would need
Binance_bid > Delta_ask >= Delta_bid > Binance_ask >= Binance_bid, impossible).
In a normal, uncrossed pair of markets BOTH are negative: there is nothing to
take, however far apart the mids are.

Why the chart line is the MID spread, not the "net_spread" in the original
prompt: that prompt's rule was

    net = s1 if s1 >= 0 else -s2

In a normal market s2 is negative, so -s2 is POSITIVE — the line would sit
above zero almost all the time and look like a permanent opportunity in the
WRONG direction. The honest continuous oscillator is

    mid_spread = Binance_mid - Delta_mid

drawn inside its ribbon [s1, -s2] (= mid -/+ both half-spreads). The trade is
executable only when the WHOLE ribbon is off zero, and worth taking only when
it clears the fee line. `edge` / `edge_net` below are the signed executable
numbers: zero unless a direction is actually positive.
"""

from dataclasses import dataclass, asdict

BUY_DELTA = "BUY_DELTA_SELL_BINANCE"
BUY_BINANCE = "BUY_BINANCE_SELL_DELTA"
LABEL = {BUY_DELTA: "BUY DELTA → SELL BINANCE", BUY_BINANCE: "BUY BINANCE → SELL DELTA", None: "—"}


@dataclass(frozen=True)
class Spread:
    # inputs
    d_bid: float
    d_ask: float
    b_bid: float
    b_ask: float
    # mids
    d_mid: float
    b_mid: float
    mid_spread: float           # b_mid - d_mid
    mid_spread_pct: float       # / d_mid * 100
    # the two executable directions, gross
    s1: float                   # b_bid - d_ask   (buy Delta / sell Binance)
    s1_pct: float               # / d_ask * 100
    s2: float                   # d_bid - b_ask   (buy Binance / sell Delta)
    s2_pct: float               # / b_ask * 100
    # fees (USD per BTC, for the configured FEE_MODE) and net per direction
    fees1: float
    fees2: float
    net1: float                 # s1 - fees1
    net2: float                 # s2 - fees2
    # the better of the two directions (the one closer to paying), even if negative
    direction: str
    gross: float
    fees: float
    net: float
    gross_pct: float
    net_pct: float
    # signed executable edge: 0 unless a direction is actually positive
    edge: float                 # +s1 if s1>0, -s2 if s2>0, else 0
    edge_net: float             # +net1 if net1>0, -net2 if net2>0, else 0

    def as_dict(self) -> dict:
        return asdict(self)


def compute(d_bid: float, d_ask: float, b_bid: float, b_ask: float, fee_model) -> Spread:
    d_mid, b_mid = (d_bid + d_ask) / 2, (b_bid + b_ask) / 2
    s1, s2 = b_bid - d_ask, d_bid - b_ask
    fees1, fees2 = fee_model.cost(d_ask, b_bid), fee_model.cost(d_bid, b_ask)
    net1, net2 = s1 - fees1, s2 - fees2

    if s1 >= s2:
        direction, gross, fees, net, base = BUY_DELTA, s1, fees1, net1, d_ask
    else:
        direction, gross, fees, net, base = BUY_BINANCE, s2, fees2, net2, b_ask

    edge = s1 if s1 > 0 else (-s2 if s2 > 0 else 0.0)
    edge_net = net1 if net1 > 0 else (-net2 if net2 > 0 else 0.0)

    return Spread(
        d_bid=d_bid, d_ask=d_ask, b_bid=b_bid, b_ask=b_ask,
        d_mid=d_mid, b_mid=b_mid,
        mid_spread=b_mid - d_mid, mid_spread_pct=(b_mid - d_mid) / d_mid * 100,
        s1=s1, s1_pct=s1 / d_ask * 100, s2=s2, s2_pct=s2 / b_ask * 100,
        fees1=fees1, fees2=fees2, net1=net1, net2=net2,
        direction=direction, gross=gross, fees=fees, net=net,
        gross_pct=gross / base * 100, net_pct=net / base * 100,
        edge=edge, edge_net=edge_net,
    )


def threshold_usd(price: float, thr_usd: float, thr_pct: float) -> float:
    """The net spread must clear BOTH the dollar and the percent threshold."""
    return max(thr_usd, price * thr_pct / 100)


def quality(d_age_ms, b_age_ms, d_connected: bool, b_connected: bool,
            max_age_ms: float, max_diff_ms: float) -> str:
    """OK | NO_DATA | DISCONNECTED | STALE | DESYNC. Only OK may be an opportunity.

    Ages are measured on OUR monotonic clock from receipt, so an exchange whose
    clock is off cannot make a stale quote look fresh."""
    if d_age_ms is None or b_age_ms is None:
        return "NO_DATA"
    if not (d_connected and b_connected):
        return "DISCONNECTED"
    if d_age_ms > max_age_ms or b_age_ms > max_age_ms:
        return "STALE"
    if abs(d_age_ms - b_age_ms) > max_diff_ms:
        return "DESYNC"
    return "OK"


def size_ok(direction: str, d_bid_qty, d_ask_qty, b_bid_qty, b_ask_qty, size_btc: float) -> bool:
    """Top-of-book only (v1): is there at least `size_btc` on both sides we would hit?"""
    if direction == BUY_DELTA:
        return (d_ask_qty or 0) >= size_btc and (b_bid_qty or 0) >= size_btc
    return (b_ask_qty or 0) >= size_btc and (d_bid_qty or 0) >= size_btc
