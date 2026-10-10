@echo off
cd /d C:\trading\fyers_data_pipeline
"C:\trading\fyers_data_pipeline\.venv\Scripts\python.exe" -m uvicorn deployment.main:app --host 0.0.0.0 --port 8000
