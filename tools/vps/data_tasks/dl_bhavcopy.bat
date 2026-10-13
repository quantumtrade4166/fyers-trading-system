@echo off
REM Daily: NSE equity bhavcopy, last 30 days re-checked (resume-safe). Task DataBhavcopy.
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
for /f %%d in ('powershell -NoProfile -Command "(Get-Date).AddDays(-30).ToString('yyyy-MM-dd')"') do set START=%%d
.venv\Scripts\python.exe -u backtesting\book_strategies\antonacci\download_bhavcopy.py --start %START% >> logs\data_bhavcopy.log 2>&1
