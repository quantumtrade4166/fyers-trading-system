@echo off
REM ── Delta-Neutral Strangle engine launcher ──────────────────────────────
REM Run by the Windows scheduled task "DeltaNeutralEngine" at 09:20 on weekdays,
REM and by the dashboard watchdog (_ensure_dn_running) if it dies mid-day.
REM
REM USES THE VENV PYTHON. This is the opposite of the local dev machine, where
REM fyers_apiv3 sits on system python — on the VPS the venv is the interpreter
REM that has pandas / kiteconnect, and every other VPS launcher (dashboard,
REM fyers_auto_login, zerodha_auto_login, V2 engine) uses it too. Launching this
REM with bare `python` crashed on ModuleNotFoundError at the first VPS run.
REM
REM The engine takes market data from KITE, not Fyers, so it never competes with
REM the VWAP strangle's Fyers socket.
REM
REM Task created with:
REM   schtasks /Create /TN DeltaNeutralEngine /TR "<this file>" ^
REM     /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /RL HIGHEST /F
REM Hardened afterwards: start-when-missed, restart 3x/1min on failure, and NO
REM execution time limit (that setting once killed a live engine mid-session).

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
.venv\Scripts\python.exe -u live_trading_options\delta_neutral\engine.py >> logs\dn_engine.log 2>&1
