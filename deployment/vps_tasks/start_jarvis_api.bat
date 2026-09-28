@echo off
REM start_jarvis_api.bat — Launch JARVIS API server on port 8081
REM
REM This is a SEPARATE process from the dashboard. It runs the JARVIS
REM REST API (agent loop, VPS control, command execution) on port 8081.
REM The dashboard stays on port 8000.
REM
REM Logs go to logs\jarvis_api.log

REM Binds to 127.0.0.1 only: reach it through the named Cloudflare tunnel,
REM never the public network card. Callers must send an API key or token.

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
echo [%date% %time%] Starting JARVIS API server... >> logs\jarvis_api.log
REM Clear stale bytecode so the latest code always loads
if exist jarvis\__pycache__ rd /s /q jarvis\__pycache__
if exist jarvis\api\__pycache__ rd /s /q jarvis\api\__pycache__
"C:\Users\Administrator\Desktop\fyers_data_pipeline_git\.venv\Scripts\python.exe" -m uvicorn jarvis.api.server:app --host 127.0.0.1 --port 8081 >> logs\jarvis_api.log 2>&1
