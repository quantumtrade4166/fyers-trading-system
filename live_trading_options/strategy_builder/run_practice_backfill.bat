@echo off
REM -- Practice-day backfill (Strategy Builder practice mode) ------------------------
REM Task "PracticeBackfill": weekdays 16:30 IST (adds the new day) + run once by hand for 2026.
REM Downloads every missing weekday since 2026-01-01, both indices, newest first, ONE Breeze worker.
REM Skips days already packed and holidays; read-only market data, places no orders.
cd /d C:	radingyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u -m live_trading_options.strategy_builder.practice_data --backfill 2026-01-01 today >> logs\practice_backfill.log 2>&1
