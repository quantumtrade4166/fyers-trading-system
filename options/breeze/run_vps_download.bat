@echo off
REM ===================================================================
REM  VPS daily Breeze download - fully unattended.
REM
REM  Differs from resume_download.bat (the local, interactive one):
REM    * logs in headlessly via auto_login.py instead of asking a human
REM    * 1-MINUTE data, not 1-second. 1-min is one API call per
REM      contract-day vs 24 for 1-second, so the full 2021-2026 chain
REM      is ~3 weeks of downloading instead of ~2 years.
REM    * front + next expiry, which is the whole point: the local Fyers
REM      dataset is front-week only, and that is what forced the credit
REM      spread strategy to discard 72% of its signals.
REM    * no pause - it runs from Task Scheduler
REM
REM  --ignore-daily-cap disables OUR 5,000/day ceiling so the SERVER
REM  becomes the limit. That is deliberate: the documented cap has not
REM  been tested, and this is how we find the real one.
REM ===================================================================

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git

echo [%date% %time%] Breeze auto-login...
.venv\Scripts\python.exe -m options.breeze.auto_login
if errorlevel 1 (
    echo [%date% %time%] LOGIN FAILED - nothing downloaded today.
    exit /b 1
)

echo [%date% %time%] Reconciling manifest against files on disk...
.venv\Scripts\python.exe -m options.breeze.reconcile --fix

echo [%date% %time%] Resuming NIFTY 1-minute download...
.venv\Scripts\python.exe -u -m options.breeze.downloader ^
    --start 2021-01-01 --end 2026-05-31 ^
    --interval 1minute --expiries 2 --strikes 10 ^
    --ignore-daily-cap

echo [%date% %time%] Run ended. Dataset status:
.venv\Scripts\python.exe -m options.breeze.status
