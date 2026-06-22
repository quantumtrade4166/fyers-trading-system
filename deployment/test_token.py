import sys, json, os
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

token_path = Path(os.getenv("ACCESS_TOKEN_PATH", r"G:\fyers_data_pipeline\config\access_token.txt"))
print(f"TOKEN_PATH = {token_path}")
print(f"EXISTS     = {token_path.exists()}")

if token_path.exists():
    raw = token_path.read_text(encoding="utf-8").strip()
    print(f"RAW_LEN    = {len(raw)}")
    try:
        d = json.loads(raw)
        tok = d.get("token", "")
        print(f"DATE       = {d.get('date')}")
        print(f"TOKEN_LEN  = {len(tok)}")
        print(f"TOKEN_PRE  = {tok[:20]}")
    except Exception as e:
        print(f"JSON_ERR   = {e}")
        print(f"RAW_PRE    = {raw[:50]}")
