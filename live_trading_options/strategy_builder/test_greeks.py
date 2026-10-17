"""Offline checks for greeks.py.  Run:  .venv\\Scripts\\python.exe -m live_trading_options.strategy_builder.test_greeks"""
import sys
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from live_trading_options.strategy_builder import greeks as g

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


T = 3 / 365
F = 23100.0

# put-call parity on the forward: C - P = F - K
c = g.price(F, 23000, T, 0.11, True)
p = g.price(F, 23000, T, 0.11, False)
check("parity C-P = F-K", abs((c - p) - 100.0) < 1e-6)

# IV round trip, vectorised, both sides
K = np.array([22500, 23000, 23100, 23200, 23700.0])
for call in (True, False):
    prem = g.price(F, K, T, 0.13, call)
    iv = g.implied_vol(prem, F, K, T, call)
    check(f"iv round-trip {'CE' if call else 'PE'}", np.allclose(iv, 0.13, atol=1e-6))

# premium below intrinsic has no vol
check("below intrinsic -> NaN", np.isnan(g.implied_vol(50.0, F, 23000, T, True)))
check("zero premium -> NaN", np.isnan(g.implied_vol(0.0, F, 23000, T, True)))

# Fyers sample (16-Oct close, 19-Oct expiry): 23000 CE ltp 144 -> Fyers iv 11.13, delta 0.66
T_f = (3 * 24 * 60 + 6 * 60 + 15) / (365 * 24 * 60)        # Fri 15:30 -> Mon 15:30 ~ 3 days
iv = float(g.implied_vol(144.0, 23090.0, 23000, T_f, True))
d = float(g.greeks(23090.0, 23000, T_f, iv, True)["delta"])
check(f"Fyers sample iv {iv*100:.2f}% in 8..16", 0.08 < iv < 0.16)
check(f"Fyers sample delta {d:.2f} near 0.66", abs(d - 0.66) < 0.08)

# greek signs
gr = g.greeks(F, 23100, T, 0.11, True)
check("ATM call delta ~0.5", abs(float(gr["delta"]) - 0.5) < 0.02)
check("theta negative", float(gr["theta"]) < 0)
check("vega positive", float(gr["vega"]) > 0)
check("put delta = call delta - 1",
      abs(float(g.greeks(F, 23100, T, 0.11, False)["delta"]) - (float(gr["delta"]) - 1)) < 1e-12)

# expiry: price -> intrinsic
check("at expiry price = intrinsic", abs(float(g.price(F, 23000, 0.0, 0.11, True)) - 100.0) < 0.01)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
sys.exit(1 if FAILS else 0)
