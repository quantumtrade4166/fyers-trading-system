# DualMom.Liq.Nifty50 — Kotak Neo live deployment

**Status: BUILT, NOT LIVE.** `config.ENABLED = False`, `config.DRY_RUN = True`,
nothing imports this package from `deployment/main.py`, and no scheduler job exists.
Built 2026-09-07 with the market open and the Vwap Strangle trading — deliberately inert.

Tests: **89 passing, 0 failing** — `test_offline` (52) + `test_parity` (37).
No broker, no token, no network.

---

## ✅ Session hazard — RESOLVED by a separate account

Kotak Neo allows one session **per account**. The Vwap Strangle mirror holds the
strangle account's session all day. **DualMom uses a DIFFERENT Kotak account**
(confirmed 2026-09-07), so it has its own session and cannot disturb the strangle.

That safety rests entirely on the credentials being different:

```
deployment/.env
    KOTAK_DM_CONSUMER_KEY      <- DualMom account
    KOTAK_DM_MOBILE
    KOTAK_DM_UCC
    KOTAK_DM_MPIN
    KOTAK_DM_TOTP_SECRET
```

`kotak_auth_dm.login()` **refuses to start** if any `KOTAK_DM_*` value equals the
strangle's `KOTAK_*` equivalent — a copy-paste there would steal the strangle's
session mid-day.

---

## 🧪 Verification: 89 tests, 0 failures

| Suite | Tests | Proves |
|---|---|---|
| `tests/test_offline.py` | **52** | order layer against a stub broker that REFUSES |
| `tests/test_parity.py` | **37** | live decisions are **byte-identical** to the validated backtest |

Parity walks real month-ends (2008-01, 2013-06, 2018-09, 2021-01, 2024-03,
2026-08, 2026-09) and asserts the same IN/OUT call, the same top-40 basket, the
same weights to 1e-9, and the **same integer share counts** as `dualmom_final.py`
— the run that produced CAGR 33.10% / DD −30.3%.

### Four defects parity caught that no happy-path test would

1. **Sizing used the limit buffer (0.5%) instead of expected slippage (0.1%).**
   The backtest sizes off 0.1%. A marketable limit is a *cap*, not the price paid.
   Sizing off the cap under-buys ~0.4% every rebalance — a permanent drag.
   Split into `SIZING_SLIPPAGE` (0.001, matches backtest) and `MARKETABLE_BUFFER` (0.005, the cap).
2. **Weights were rounded to 6dp** in the signal JSON, so live fed different
   weights into `allocate()` than the backtest (4.9e-07). Full precision now.
3. **The universe guard was an absolute floor (475).** It cannot tell "the
   download failed" from "fewer companies were listed in 2008" — it blocked every
   historical date while a half-failed download of today's 484 would pass at 400.
   Now **relative**: at least 90% of the trailing-60-day norm, using the *same*
   validity rule (today's price AND the 252-back price — the first version
   compared 223 valid against 259 same-day and falsely blocked Jan-2008).
4. **A hard 20% weight ceiling REFUSED the whole rebalance.** On 2021-01-29
   PATANJALI was 35.9%; a ceiling would have blocked that month with no defined
   fallback, leaving a stale book. Live must reproduce the validated (uncapped)
   backtest, so the ceiling is now `None` + a warning at 15%. **Adopting a cap is
   a strategy change and must be re-measured in `dualmom_final.py` first.**

---

## Files

| File | Does |
|---|---|
| `config.py` | All settings. Master `ENABLED` / `DRY_RUN` switches |
| `signal_engine.py` | Month-end signal from **Fyers** data. Hard universe guard |
| `rebalance.py` | `allocate()` (whole shares, 2× cap), `plan()` (delta), `stop_breaches()` |
| `kotak_equity.py` | Kotak `nse_cm` / CNC order layer, holdings, cash |
| `runner.py` | Ties it together. Sells → confirm → re-read cash → buys |
| `kotak_auth_dm.py` | Login for the **separate** DualMom Kotak account |
| `tests/test_offline.py` | 52 tests vs a stub broker that **refuses** |
| `tests/test_parity.py` | 37 tests vs the canonical backtest |

---

## What is deliberately different from `dualmom_paper.py`

The paper engine is **not** a base to build on — it contradicts the final spec:

| | paper engine | this package |
|---|---|---|
| Data | yfinance | **Fyers** |
| TOP_N | 50 | **40** |
| Shares | fractional | **whole** |
| Universe guard | `len < 10` | **relative: 90% of trailing norm** |
| Stop-loss | none | **−35% intra-month** |

yfinance is banned: zero rows for `^NSEI` at 16:00 on 2026-08-31, 723 missing index
days, and 0 of 6 corporate actions adjusted.

---

## Order-safety rules carried from [[Live Order Safety]]

1. **Never write a fill you have not seen** — only `order_status()` closes a leg;
   `await_fill()` polls to a terminal state. "Placed" is not "filled".
2. **A rejection is DATA** — `place_limit()` returns `{status: REJECTED, reason}`,
   never raises on a broker refusal.
3. **Margin → halt.** `is_margin_error()` classifies; the run stops rather than
   retrying into the same wall.
4. **Tag everything** — `dualmom`, deliberately distinct from `vwstrangle`,
   `dnstrangle`, `vwstk_kotak`. Two strategies must never share a tag.
5. **Marketable limit, not market.** Equity *accepts* market orders, but 40 orders
   into recently-mooned small caps is where the slippage lives.

### T+1 ordering is not a preference
Sell proceeds are not fully available as buying power the same day. `runner.execute()`
places all sells → confirms → **re-reads the broker's actual cash** → sizes buys
against that. Buys that do not fit are deferred, not force-fired.

---

## ⚠️ Operational change: this is no longer a monthly strategy

The −35% stop needs a **daily EOD position check** (`config.STOP_CHECK = "15:25"`),
on top of the monthly rebalance. That is a new unattended job that can place sell
orders. It did not exist in the monthly design.

---

## Still to build

1. **Own-book ledger + restart reconciliation.** Holdings carry no tag; the account
   being dedicated is what currently makes NAV trustworthy.
2. **Scheduler jobs** — signal 15:20 month-end, execute 09:20 next day, stop check 15:25.
3. **Dashboard tab** — per-broker equity curves, capital input, deploy button.
4. **Zerodha equity leg** (second client account) — same shape, different executor.
5. **`resolve()` is unverified for `nse_cm`.** Written defensively across key
   spellings; the first real call is self-documenting. **Verify before any live order.**
6. **Tag round-trip unconfirmed** — it is not proven Kotak echoes `tag` back through
   `order_report`. If it does not, scope by `trading_symbol` instead.
7. **Daily Fyers data refresh.** The parquets currently end 2026-09-01; a live signal
   needs them current to the rebalance date.

---

## Open questions blocking the build

1. ~~Session policy~~ ✅ **resolved — separate Kotak account**
2. Is Zerodha = client 1 and Kotak = client 2?
3. Capital in the DualMom Kotak account?
4. Actual brokerage rate card — swings expected CAGR by ~1 point, and DP charges
   alone are 0.39%/yr at Rs 10L.
5. Concentration cap: adopt one (needs a backtest re-run) or accept 35.9% tails?

---

## Go-live sequence — each step gated on the previous

| # | Step | Gate |
|---|---|---|
| 1 | Add `KOTAK_DM_*` creds to `deployment/.env` | `python -m deployment.dualmom_live.kotak_auth_dm` prints login OK |
| 2 | **After market close**, verify `resolve()` on ~10 real equity symbols | returned `trading_symbol` shape matches expectation |
| 3 | Verify `holdings()` + `cash_available()` against the Kotak app | numbers agree to the rupee |
| 4 | Refresh the Fyers parquets to the rebalance date | `signal_engine` universe count is current |
| 5 | Dry run a real month-end with the live client, `DRY_RUN=True` | plan matches the backtest basket for that date |
| 6 | Place **ONE** small manual test order via `place_limit`, then cancel | id parsed, status parsed, `tag` round-trips in `order_report` |
| 7 | One **supervised** rebalance, `force=True`, watched end to end | every fill confirmed; own book equals broker holdings |
| 8 | Flip `ENABLED=True`, attach scheduler jobs | monitored for a full month |

**Do not skip step 6.** The `tag` round-trip is unproven, and without it the book
cannot be scoped to our own orders.
