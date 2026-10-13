@echo off
REM Daily: Dukascopy 1-min, 11 instruments; re-downloads the last 2 months (the current one is
REM always partial). Task DataDukascopy1m. Needs Node.js + forex\node_modules (npm ci).
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
REM Task Scheduler may hold a PATH from before Node was installed
set "PATH=%PATH%;C:\Program Files\nodejs"
set DUKASCOPY_1M_DIR=C:\trading\fyers_data_pipeline\forex\Dukascopy_1m
.venv\Scripts\python.exe -u forex\download_basket_1m.py --refresh-recent 2 >> logs\data_dukascopy_1m.log 2>&1
