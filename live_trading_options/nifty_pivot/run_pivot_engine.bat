@echo off
REM ── Nifty Directional Pivot engine launcher (PAPER) ─────────────────────────
REM Run by the Windows scheduled task "NiftyPivotEngine" at 09:10 IST on weekdays,
REM and by the dashboard watchdog (_ensure_ndp_running) if it dies mid-session.
REM
REM Uses the VENV python — on the VPS that is the interpreter with pandas and
REM kiteconnect (bare `python` crashed the delta-neutral engine's first VPS run).
REM
REM Market data comes from KITE, never a Fyers socket, so it cannot compete with
REM the VWAP strangle's Fyers feed. Paper only: it places no orders.
REM
REM Task created with:
REM   schtasks /Create /TN NiftyPivotEngine /TR "<this file>" ^
REM     /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:10 /RL HIGHEST /F
REM then hardened: start-when-missed, restart on failure, NO execution time limit.

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
if not exist logs mkdir logs
.venv\Scripts\python.exe -u live_trading_options\nifty_pivot\engine.py >> logs\nifty_pivot_engine.log 2>&1
