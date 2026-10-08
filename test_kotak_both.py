"""Dry-login test for BOTH Kotak accounts (Bhaiya + Rohit). Run on VPS with venv Python."""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

# Ensure we can find the module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live_trading_options.strangle_strategy.live.kotak_auth import login

print("=== Bhaiya login ===")
try:
    c = login(verbose=True, rohit=False)
    print("BHAIYA OK")
except Exception as e:
    print(f"BHAIYA FAIL: {type(e).__name__}: {e}")

print()
print("=== Rohit login ===")
try:
    c = login(verbose=True, rohit=True)
    print("ROHIT OK")
except Exception as e:
    print(f"ROHIT FAIL: {type(e).__name__}: {e}")
