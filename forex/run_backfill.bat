@echo off
cd /d G:\fyers_data_pipeline
"G:\fyers_data_pipeline\.venv\Scripts\python.exe" "G:\fyers_data_pipeline\forex\download_xauusd_1s.py" >> "G:\fyers_data_pipeline\logs\xauusd_backfill_console.log" 2>&1
