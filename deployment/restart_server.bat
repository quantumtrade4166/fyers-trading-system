@echo off
REM Restarts the DASHBOARD only. See restart_server.ps1 for why this no longer
REM kills every python process, and for the -All switch (refused in market hours).
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\restart_server.ps1" %*
