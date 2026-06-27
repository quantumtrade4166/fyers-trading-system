@echo off
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe" -m uvicorn deployment.main:app --host 0.0.0.0 --port 8000
