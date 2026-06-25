"""
zerodha_login.py
Daily Kite Connect login helper. Run once per trading day:

    G:\\fyers_data_pipeline\\.venv\\Scripts\\python.exe deployment\\brokers\\zerodha_login.py

It prints the Kite login URL. Log in, copy the `request_token` from the
redirect URL, paste it here, and it writes deployment/zerodha_token.json.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import json
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

API_KEY    = os.getenv("KITE_API_KEY", "")
API_SECRET = os.getenv("KITE_API_SECRET", "")
TOKEN_FILE = Path(__file__).parent.parent / "zerodha_token.json"


def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: set KITE_API_KEY and KITE_API_SECRET in deployment/.env first.")
        sys.exit(1)

    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=API_KEY)
    print("\n1. Open this URL, log in to Zerodha:\n")
    print("   " + kite.login_url() + "\n")
    print("2. After login you'll be redirected to a URL containing  request_token=XXXX")
    request_token = input("3. Paste the request_token here: ").strip()

    data = kite.generate_session(request_token, api_secret=API_SECRET)
    access_token = data["access_token"]

    TOKEN_FILE.write_text(json.dumps({
        "access_token": access_token,
        "date":         date.today().isoformat(),
    }, indent=2), encoding="utf-8")
    print(f"\n✅ Saved access token for {date.today().isoformat()} → {TOKEN_FILE}")


if __name__ == "__main__":
    main()
