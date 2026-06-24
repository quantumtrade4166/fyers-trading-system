@echo off
REM Strangle-system morning trade/no-trade flag (VPS).
REM Scheduled ~09:10 IST (after 9:00 FyersAutoLogin token refresh), pre-open.
REM Uses the most recent EOD chain snapshot (point-in-time). Paper-log only
REM until the VRP-validation backtest passes. Writes flags/ + paper_signal_log.csv.
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
.venv\Scripts\python.exe -m strangle_system.decision_runner >> logs\strangle_flag.log 2>&1
