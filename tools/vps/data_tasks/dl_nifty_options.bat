@echo off
REM Daily: 1-min bars of every ACTIVE NIFTY option, ATM +-10, 4 expiries (Fyers serves only
REM unexpired contracts, so this must run every day). Task DataNiftyOptions1m.
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u options\run_options_pipeline.py >> logs\data_nifty_options.log 2>&1
