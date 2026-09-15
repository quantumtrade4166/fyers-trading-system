@echo off
REM Double-click: installs the DualMom client ledger + live portfolio dashboard.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_dualmom_portfolio.ps1"
pause
