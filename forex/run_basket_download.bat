@echo off
cd /d G:\fyers_data_pipeline
"G:\fyers_data_pipeline\.venv\Scripts\python.exe" "G:\fyers_data_pipeline\forex\download_basket_1m.py" >> "G:\fyers_data_pipeline\logs\basket_console.log" 2>&1
