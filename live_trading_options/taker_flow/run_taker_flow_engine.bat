@echo off
REM -- Taker Flow L/S engine launcher (PAPER) ----------------------------------
REM Run by the scheduled task "TakerFlowEngine" at boot and every 5 minutes; a
REM second copy exits at once (lock port 47678), so the 5-minute trigger is the
REM watchdog. Crypto trades 24/7: the engine never stops on its own.
REM Paper only: Binance public data + Delta public order books, no API keys, no orders.

cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u -m live_trading_options.taker_flow.engine >> logs\taker_flow_engine.log 2>&1
