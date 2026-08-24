@echo off
REM ── Delta-Neutral Strangle engine launcher ──────────────────────────────
REM Run by the Windows scheduled task "DeltaNeutralEngine" at 09:20 on weekdays,
REM and by the dashboard watchdog (_ensure_dn_running) if it ever dies mid-day.
REM
REM Uses SYSTEM python, not .venv — fyers_apiv3 is installed there, same as the
REM options pipeline and the V2 tick engine.
REM
REM Create the scheduled task once (run as Administrator):
REM   schtasks /Create /TN DeltaNeutralEngine /TR "G:\fyers_data_pipeline\live_trading_options\delta_neutral\run_dn_engine.bat" ^
REM     /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 09:20 /RL HIGHEST /F
REM Then, matching how the other trading tasks are hardened:
REM   - Settings: allow "Run task as soon as possible after a scheduled start is missed"
REM   - Settings: "If the task fails, restart every 1 minute, up to 3 times"
REM   - Settings: clear "Stop the task if it runs longer than ..." (the 72h kill
REM     silently killed a live engine once — see the VPS hardening notes)

cd /d G:\fyers_data_pipeline
python live_trading_options\delta_neutral\engine.py >> logs\dn_engine.log 2>&1
