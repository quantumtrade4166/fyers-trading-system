@echo off
REM Double-click: fixes the rejected-orders problem (runs fix_dualmom_orders.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_dualmom_orders.ps1"
pause
