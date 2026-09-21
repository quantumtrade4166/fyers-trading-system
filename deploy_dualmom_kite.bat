@echo off
REM Double-click: installs DualMom on the main Kite account, previews the Rs 6,00,000
REM basket, and places the orders ONLY after you type DEPLOY.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_dualmom_kite.ps1"
pause
