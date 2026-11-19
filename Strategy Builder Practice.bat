@echo off
REM Strategy Builder - PRACTICE ONLY, private on this PC (http://localhost:47675).
REM No Fyers feed, no broker logins, no live desk: it replays practice days and fills on paper.
REM 1) copies any new / rebuilt practice days from the VPS (3 ssh calls)  2) starts the builder  3) opens the browser.
REM Keep this window open while you practise; closing it stops the private builder.
title Strategy Builder PRACTICE (private) - keep this window open
cd /d "G:\fyers_data_pipeline"
if errorlevel 1 (
  echo Could not open G:\fyers_data_pipeline - is the G: drive connected?
  pause
  exit /b 1
)
echo Copying new practice days from the VPS...
".venv\Scripts\python.exe" -m live_trading_options.strategy_builder.practice_sync
set SB_PRACTICE_ONLY=1
set SB_PORT=47675
echo.
echo Practice builder: http://localhost:47675   (only this PC can open it)
echo The browser opens by itself in a few seconds - if it shows "refused", wait 5 s and press F5.
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://localhost:47675/"
".venv\Scripts\python.exe" -m live_trading_options.strategy_builder.service
echo.
echo The practice builder stopped (see the message above).
pause
