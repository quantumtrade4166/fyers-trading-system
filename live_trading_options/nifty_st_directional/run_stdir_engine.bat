@echo off
REM -- Nifty ST Directional engine launcher (PAPER) ---------------------------
REM Run by the scheduled task "NiftyStDirEngine" at 09:10 IST on weekdays (after the 09:00 Kite + Fyers logins), and by
REM the dashboard watchdog (_ensure_stdir_running) if it dies mid-session.
REM Market data from KITE (its own ticker), Fyers REST history for warm-up only.
REM Paper only: it places no orders.

cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u live_trading_options\nifty_st_directional\engine.py >> logs\nifty_stdir_engine.log 2>&1
