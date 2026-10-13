@echo off
REM Daily: 5-min OHLCV for the 182 NIFTY F&O stocks (Fyers, incremental). Task DataNiftyStocks5m.
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u run_pipeline.py --mode update >> logs\data_nifty_stocks.log 2>&1
