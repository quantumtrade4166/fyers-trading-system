@echo off
cd /d C:\trading\fyers_data_pipeline
C:\trading\fyers_data_pipeline\.venv\Scripts\python.exe C:\trading\fyers_data_pipeline\tools\convert_shrajan_options.py >> C:\trading\fyers_data_pipeline\data\SHRAJAN_OPTIONS\convert.log 2>&1
