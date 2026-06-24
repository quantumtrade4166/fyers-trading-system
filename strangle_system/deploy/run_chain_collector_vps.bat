@echo off
REM Strangle-system daily chain snapshot capture (VPS).
REM Scheduled at 15:05 / 15:15 / 15:25 IST (multi-capture, latest-wins).
REM Drive push stays ENABLED: it no-ops until config/gdrive_credentials.json
REM exists, then auto-activates. Logs append for audit.
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
.venv\Scripts\python.exe -m strangle_system.data.chain_collector >> logs\chain_collector_sched.log 2>&1
