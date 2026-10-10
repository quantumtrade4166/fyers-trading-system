@echo off
cd /d C:\trading\fyers_data_pipeline
C:\trading\fyers_data_pipeline\.venv\Scripts\python.exe forex\download_nasdaq_1m.py > forex\logs\nasdaq_download.log 2>&1
echo DONE >> forex\logs\nasdaq_download.log
