@echo off
REM ── Square-off watchdog ─────────────────────────────────────────────────
REM Runs at 15:20 weekdays, six minutes after the engines' own 15:14 square-off.
REM
REM Both strangles now trade NRML, which Zerodha does NOT auto-square-off. The
REM engines' 15:14 exit is therefore the only thing closing a position — and an
REM engine that has died, stalled, or been silently disabled will not run it.
REM This process is independent of both engines: it asks the broker what is still
REM short, matches it against our own tagged orders, and closes what is ours.
REM
REM Read logs\squareoff_watchdog.log after any day that ended oddly.

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
echo ================================================== >> logs\squareoff_watchdog.log
.venv\Scripts\python.exe -u live_trading_options\tools\squareoff_watchdog.py >> logs\squareoff_watchdog.log 2>&1
