@echo off
taskkill /F /IM python.exe /T 2>nul
timeout /t 2 /nobreak >nul
schtasks /Run /TN PairsDashboard
echo Server restarted via Task Scheduler.
