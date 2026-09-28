@echo off
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe forex\download_nasdaq_1m.py > forex\logs\nasdaq_download.log 2>&1
echo DONE >> forex\logs\nasdaq_download.log
