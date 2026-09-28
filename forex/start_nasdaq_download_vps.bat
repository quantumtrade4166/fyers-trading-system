@echo off
REM Start Nasdaq 100 download on VPS as a background task
REM This script runs the Python downloader, logs output, and stays alive

cd /d "C:\Users\Administrator\Desktop\fyers_data_pipeline_git"

REM Use full path to venv Python
set PYTHON=C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe

REM Create logs directory if missing
if not exist "forex\logs" mkdir forex\logs

REM Run the downloader in background, output to log
start /b "" "%PYTHON%" forex\download_nasdaq_1m.py > forex\logs\nasdaq_download.log 2>&1

echo Download started in background
echo Log: forex\logs\nasdaq_download.log
