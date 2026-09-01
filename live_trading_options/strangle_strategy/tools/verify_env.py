"""tools/verify_env.py — read-only import check of the engine venv. Places nothing, changes nothing."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

for m in ["numpy", "urllib3", "six", "dotenv", "pandas", "requests",
          "kiteconnect", "pyotp", "neo_api_client", "fyers_apiv3"]:
    try:
        mod = __import__(m)
        print(f"OK   {m}=={getattr(mod, '__version__', '?')}")
    except Exception as e:
        print(f"FAIL {m}: {type(e).__name__}: {e}")
