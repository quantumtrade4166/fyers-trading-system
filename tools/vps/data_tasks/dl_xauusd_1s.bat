@echo off
REM Daily: Dukascopy XAUUSD 1-second; re-downloads the last 2 months. Task DataXauusd1s.
REM Needs Node.js + forex\node_modules (npm ci).
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
REM Task Scheduler may hold a PATH from before Node was installed
set "PATH=%PATH%;C:\Program Files\nodejs"
set XAUUSD_1S_DIR=C:\trading\fyers_data_pipeline\forex\XAUUSD_1s_Data
.venv\Scripts\python.exe -u forex\download_xauusd_1s.py --refresh-recent 2 >> logs\data_xauusd_1s.log 2>&1
