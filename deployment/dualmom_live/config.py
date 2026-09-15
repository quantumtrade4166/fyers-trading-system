"""
dualmom_live/config.py — DualMom.Liq.Nifty50 live deployment settings.

⚠️ NOTHING HERE IS LIVE. `ENABLED = False` and no module in this package is
imported by deployment/main.py. Built 2026-09-07 while the market was open and
the Vwap Strangle was trading — deliberately inert.

Strategy rules are FINAL as of the 2026-09-02 review; see
[[DualMom_Liq_Nifty50_Results]]. Backtest with these exact settings:
CAGR 33.10%, Sharpe 1.577, max daily drawdown -30.3%.
Realistic expectation after survivorship (-2.95 pts) and costs: ~29% CAGR.
"""

# ── master switch ────────────────────────────────────────────────────────────
# ARMED 2026-09-15 after go-live step 6 passed live (1 IDEA via 103.49.131.3:
# placed, filled 1 @ 14.78, status read, tag echoed in GuiOrdId).
ENABLED = True           # nothing places an order while this is False
DRY_RUN = False          # True = build the order list and log it, never send

# ── strategy (FINAL — do not change without re-running dualmom_final.py) ─────
TOP_N            = 40
LOOKBACK_DAYS    = 252
MA_PERIOD        = 100          # Nifty 50 absolute-momentum filter
MAX_WEIGHT_MULT  = 2.0          # never let one share exceed 2x its target weight
STOP_LOSS_PCT    = 0.35         # intra-month exit at -35% from entry
LIQUID_FUND_PA   = 0.06         # notional rate for reporting only

# ── universe / data ──────────────────────────────────────────────────────────
# yfinance is BANNED: it returned zero rows for ^NSEI at 16:00 on 2026-08-31,
# is missing 723 Nifty index days, and adjusts 0 of 6 corporate actions.
DATA_SOURCE      = "fyers"
NIFTY_INDEX_SYM  = "NSE:NIFTY50-INDEX"
# The guard must detect a FAILED DOWNLOAD, not a smaller historical universe.
# An absolute floor cannot tell those apart: only 223 of today's 500 names existed
# in 2008, yet a half-failed download of today's 484 would pass a floor of 400.
# So: compare against what this dataset normally carries (trailing max), and
# require most of it to be present.
UNIVERSE_REF_DAYS  = 60      # trailing window used to learn the normal count
MIN_UNIVERSE_FRAC  = 0.90    # today must have >= 90% of that
MIN_UNIVERSE_ABS   = 100     # nothing this small is ever a real universe

# ── broker: Kotak Neo ────────────────────────────────────────────────────────
BROKER            = "kotak"
EXCHANGE_SEGMENT  = "nse_cm"    # NSE cash market
PRODUCT           = "CNC"       # delivery
ORDER_TYPE        = "L"         # marketable limit; equity accepts MKT but 40
                                # orders into thin momentum names would slip
ORDER_TAG         = "dm"        # tag PREFIX - every order gets a UNIQUE tag
                                # "dm" + ddHHMMSS + NNN (kotak_equity.unique_tag).
                                # Kotak uses the tag as the client order id and
                                # rejects a repeat: the constant "dualmom" got all
                                # 40 orders rejected on 2026-09-15.
# TWO DIFFERENT NUMBERS -- conflating them silently changes position sizes.
#   SIZING_SLIPPAGE  what we EXPECT to pay. Must equal the backtest's
#                    SLIPPAGE_PCT (0.001) or live share counts diverge from the
#                    validated 33.10% run.
#   MARKETABLE_BUFFER how far THROUGH the touch the limit is priced. It is a CAP,
#                    not the price paid -- a marketable limit fills at the offer.
#                    Sizing off this would under-buy by ~0.4% every rebalance.
SIZING_SLIPPAGE   = 0.001
MARKETABLE_BUFFER = 0.005
TICK_SIZE         = 0.05

# Cash held back from sizing. Without it the September dry run (2026-09-14)
# deployed 99.90% and left Rs 999 - but every buy also pays STT 0.1%, stamp duty,
# exchange charges and Kotak brokerage (rate card still unknown), and each order is
# pre-checked at the 0.5% limit cap. The last orders would hit "insufficient funds",
# and runner.execute() HALTS the whole run on the first margin rejection.
# Cost: ~1% of NAV idle, roughly 0.3 CAGR points against the backtest's 100%.
# 2026-09-15: user set a FIXED Rs 15,000 cash reserve (in rupees, not a %). Actual
# cash left is a little above it because whole shares round down.
CASH_RESERVE_RS   = 15_000

# ── Kotak session ────────────────────────────────────────────────────────────
# Kotak allows ONE session PER ACCOUNT. DualMom uses a SEPARATE Kotak account
# from the Vwap Strangle mirror (confirmed 2026-09-07), so logging in here has
# its own session and CANNOT disturb the strangle.
#
# That safety rests entirely on the credentials being different. They live under
# a KOTAK_DM_* prefix and kotak_auth_dm.login() refuses to start if any value
# matches the strangle's KOTAK_* equivalent.
SESSION_POLICY = "own"          # safe ONLY because the account is separate
CREDENTIAL_PREFIX = "KOTAK_DM_"

# ── execution timing ─────────────────────────────────────────────────────────
SIGNAL_TIME   = "15:20"   # after the strangle's 15:14 square-off releases the session
EXECUTE_TIME  = "09:20"   # next morning; NOT 09:15 — the open is the worst
                          # slippage window for recently-mooned small caps
STOP_CHECK    = "15:25"   # daily intra-month stop check (new operational need)

# ── safety limits (hard stops on an automated run) ──────────────────────────
MAX_ORDERS_PER_RUN    = 120     # a plan bigger than this means something broke
MAX_TURNOVER_PCT      = 0.85    # refuse a plan that churns > 85% of the book
MIN_ORDER_VALUE       = 500     # skip trivial rupee-value orders
# ADOPTED 2026-09-08: no position above 10% of NAV. Momentum weighting is
# otherwise uncapped and put 35.9% into PATANJALI (Jan-2021). Excess is
# redistributed pro-rata to the uncapped names, never left idle.
# Measured cost WITH the -35% stop: -0.44 CAGR pts (33.10% -> 32.65%),
# Sharpe -0.008. Drawdown is UNCHANGED at -30.3% -- the cap buys concentration
# protection, not drawdown protection.
# MUST equal dualmom_final.MAX_WEIGHT or live diverges from the validated run.
MAX_WEIGHT            = 0.10
CONCENTRATION_WARN    = 0.15    # surfaced in the signal, never blocks
ORDER_RETRY           = 3
ORDER_POLL_SECONDS    = 2
ORDER_POLL_TIMEOUT    = 120

# ── accounts ─────────────────────────────────────────────────────────────────
# Dedicated DualMom-only account (confirmed 2026-09-01), so NAV can be read
# straight from the broker as holdings market value + cash.
ACCOUNT_IS_DEDICATED = True
CAPITAL_MODE = "account_value"   # "account_value" | "fixed"
FIXED_CAPITAL = None             # used only when CAPITAL_MODE == "fixed"

STATE_DIR = "dualmom_live_state"

# ── client record (this is a CLIENT account — see ledger.py) ─────────────────
CLIENT_ACCOUNT    = "Kotak Rohit"
CLIENT_UCC        = "15P56"
CAPITAL_BASE      = 1_000_000          # deposited before inception
INCEPTION_DATE    = "2026-09-15"       # first live entry (September basket)
BENCHMARK         = "NIFTY 50"

# Statutory + broker charges for NSE cash DELIVERY (CNC). ESTIMATES for the ledger
# only - never used for sizing. Every fill records the rate-card version it was
# costed with, so a later correction against Kotak's contract notes is traceable.
# Kotak's own brokerage for this account is NOT confirmed: brokerage_pct is 0 and
# flagged unverified until the first contract note is reconciled.
CHARGES_RATE_CARD = {
    "version": "est-2026-09-15",
    "stt_buy": 0.001,          # securities transaction tax, delivery buy
    "stt_sell": 0.001,         # delivery sell
    "stamp_buy": 0.00015,      # stamp duty on buy
    "exch_txn": 0.0000297,     # NSE transaction charge
    "sebi": 0.000001,          # SEBI turnover fee, Rs 10 / crore
    "gst": 0.18,               # on brokerage + exchange txn + SEBI fee
    "brokerage_pct": 0.0,
    "brokerage_verified": False,
}

LEDGER_CAPTURE_MIN = 5                 # market-hours capture + intraday NAV cadence
