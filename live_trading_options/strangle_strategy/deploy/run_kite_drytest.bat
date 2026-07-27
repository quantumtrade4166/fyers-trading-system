@echo off
title Kite Dry Test - Vwap Strangle
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
echo ============================================================
echo  Kite order-placement DRY TEST
echo  Places a far-below-market BUY limit (will NOT fill), then
echo  cancels it. Proves the order pipe with ~zero fill risk.
echo ============================================================
echo.
.venv\Scripts\python.exe live_trading_options\strangle_strategy\live\dry_test.py
echo.
echo ==== Dry test finished. Review the result above. ====
pause
