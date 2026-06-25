@echo off
REM Strangle execution system - daily combined-premium chart archive (VPS).
REM Schedule ~15:31 IST on weekdays. Maintains a 7-day rolling window per index.
REM Uses the VPS-generated Fyers token (refreshed automatically at 09:00 IST).
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
.venv\Scripts\python.exe live_trading_options\strangle_strategy\run_daily_archive.py >> logs\strangle_archive_sched.log 2>&1
