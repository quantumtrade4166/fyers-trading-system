@echo off
REM -- Strategy Builder service (dashboard tab "Strategy Builder") ------------------
REM Run by the scheduled task "StrategyBuilder" at boot and every 5 min (IgnoreNew, so a
REM running copy is left alone and a dead one comes back within 5 min).
REM Reads market data only (Fyers token file); places no orders in this stage.
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u -m live_trading_options.strategy_builder.service >> logs\strategy_builder_stdout.log 2>&1
