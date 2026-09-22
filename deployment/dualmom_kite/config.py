"""
dualmom_kite/config.py — DualMom.Liq.Nifty50 on the MAIN Zerodha (Kite) account.

SAME STRATEGY as the Kotak client account. Signal, weights, cap, stop, sizing and
the once-a-month gate all come from deployment.dualmom_live, so the two accounts
always hold the same basket, scaled to their own capital. Nothing strategy-level
is redefined here.

WHAT IS DIFFERENT - the account is SHARED.
    The Kotak account holds nothing but DualMom, so its NAV can be read straight
    from the broker. This Kite account also carries the Vwap Strangle, the DN
    engine and anything traded by hand. Broker cash, margin and positions are NOT
    DualMom's. So:
      * DualMom's book = only orders carrying our tag prefix (ORDER_TAG) and
        their exchange fills. Nothing else in the account is ever counted.
      * DualMom's cash = CAPITAL_BASE - buys + sells - charges (from our fills).
      * NAV = our shares at market + our cash. Margin is never cash.
      * Selling only ever touches quantities WE bought.

SESSION
    Never logs in. It reads the access token the VPS writes every morning
    (deployment/zerodha_token.json, FyersAutoLogin/Zerodha task). A second login
    would burn a TOTP attempt and could lock the account for the strangle.
"""

# ── master switch ────────────────────────────────────────────────────────────
ENABLED = True           # armed 2026-09-21 for the Rs 6L deployment (user request)
DRY_RUN = False

# ── account ──────────────────────────────────────────────────────────────────
CLIENT_ACCOUNT = "Kite Main"
BROKER         = "zerodha"
# 6.0L deployed 22-Sep 14:35 (33/37 filled, 5.37L); raised to 6.2L the same day by
# the user ("under 6 lakhs 20 thousand") and completed with a BUY-ONLY top-up.
CAPITAL_BASE   = 620_000            # allocated to DualMom, from the account's cash
INCEPTION_DATE = None               # None = date of the first DualMom fill

# Held back from sizing. 0 by user decision (2026-09-21): the full Rs 6L goes into
# the basket. Charges (~Rs 700 on 6L) and the 0.5% limit cap are covered by the
# account's OTHER cash, so DualMom's own cash may read slightly negative - that is
# the charges, and NAV includes it. (Kotak keeps Rs 15,000 on Rs 10L.)
CASH_RESERVE_RS = 0

# ── orders ───────────────────────────────────────────────────────────────────
EXCHANGE = "NSE"
PRODUCT  = "CNC"                    # delivery
# Tag PREFIX. Each order gets a unique tag "dmk" + ddHHMMSS + NNN (14 chars; Kite
# allows 20). Unique so an order whose HTTP response was lost can be FOUND by its
# tag instead of being re-sent - the only safe retry.
ORDER_TAG = "dmk"
MARKETABLE_BUFFER = 0.005           # limit priced 0.5% through the touch (a cap)
ORDER_POLL_SECONDS = 1.0
ORDER_POLL_TIMEOUT = 120

# The account also holds option margin for the strangle / DN engine. A DualMom
# buy must never leave the account below this much free margin. 0 = only require
# that the basket itself is covered. Shown on every preview either way.
MIN_FREE_MARGIN_AFTER = 0

# ── timing ───────────────────────────────────────────────────────────────────
REBALANCE_TIME = (9, 22)            # 2 min after Kotak's 09:20 - never overlap
REBALANCE_CATCHUP = (10, 5)         # if the 09:22 token was not ready yet

STATE_DIR = "dualmom_kite_state"

# ── charges (ESTIMATES, Zerodha equity delivery) ─────────────────────────────
# Brokerage is zero for delivery at Zerodha. DP charge is per scrip per SELL day.
CHARGES_RATE_CARD = {
    "version": "zerodha-est-2026-09-21",
    "stt_buy": 0.001,
    "stt_sell": 0.001,
    "stamp_buy": 0.00015,
    "exch_txn": 0.0000297,
    "sebi": 0.000001,
    "gst": 0.18,
    "brokerage_pct": 0.0,
    "brokerage_verified": True,
    "dp_per_sell_scrip_day": 15.93,   # Rs 13.50 + 18% GST
}
