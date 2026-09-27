@echo off
REM start_jarvis_api.bat — Launch JARVIS API server on port 8081
REM
REM This is a SEPARATE process from the dashboard. It runs the JARVIS
REM REST API (agent loop, VPS control, command execution) on port 8081.
REM The dashboard stays on port 8000.
REM
REM Logs go to logs\jarvis_api.log

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
echo [%date% %time%] Starting JARVIS API server... >> logs\jarvis_api.log
"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe" -m uvicorn jarvis.api.server:app --host 0.0.0.0 --port 8081 >> logs\jarvis_api.log 2>&1
