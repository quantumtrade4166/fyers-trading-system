@echo off
REM Daily 16:30 IST: re-run today's Nifty ST Directional session as a BACKTEST on ICICI data
REM (same lots + stop-to-cost as the paper engine) -> paper vs backtest bars on the dashboard.
REM Task DataStDirBacktest.
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u -m live_trading_options.nifty_st_directional.daily_backtest >> logs\stdir_daily_backtest.log 2>&1
