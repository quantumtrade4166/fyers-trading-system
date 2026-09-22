@echo off
REM Double-click: BUY-ONLY top-up of DualMom on Kite to Rs 6,20,000. Sells nothing.
REM Places orders ONLY after you type DEPLOY. Circuit-locked names wait for the next day.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0topup_dualmom_kite.ps1"
pause
