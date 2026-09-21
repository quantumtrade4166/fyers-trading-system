@echo off
REM Double-click: fixes the DualMom service (cash reading, timeouts, 72h task kill).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_dualmom_service.ps1"
pause
