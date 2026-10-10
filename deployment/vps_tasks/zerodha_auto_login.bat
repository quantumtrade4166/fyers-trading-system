@echo off
rem Kite access token via TOTP (shared by Vwap Strangle + Delta Neutral). Task ZerodhaAutoLogin, 09:00 Mon-Fri.
rem Backup for the dashboard's own 08:50 login: ensure_token() logs in ONLY if today's token is
rem missing, so it never replaces a token the engines are already using (and never burns TOTP tries).
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
"C:\trading\fyers_data_pipeline\.venv\Scripts\python.exe" -c "import sys; from deployment.brokers import zerodha_auto_login as z; sys.exit(0 if z.ensure_token() else 1)" >> logs\zerodha_auto_login.log 2>&1
