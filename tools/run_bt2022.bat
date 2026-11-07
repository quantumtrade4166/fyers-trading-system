@echo off
cd /d G:\fyers_data_pipeline
set PY=G:\fyers_data_pipeline\.venv\Scripts\python.exe
echo START BE25_SLIP05 %date% %time% > logs\bt2022.log
%PY% -m backtesting.options_st_directional.run_backtest --start 2022-01-01 --end 2022-12-31 --lots 10 --breakeven 25 --slippage 0.5 --tag Y2022_10LOTS_BE25_SLIP05 --no-day-charts >> logs\bt2022.log 2>&1
echo START SLIP05 %date% %time% >> logs\bt2022.log
%PY% -m backtesting.options_st_directional.run_backtest --start 2022-01-01 --end 2022-12-31 --lots 10 --slippage 0.5 --tag Y2022_10LOTS_SLIP05 --no-day-charts >> logs\bt2022.log 2>&1
echo ALL DONE %date% %time% >> logs\bt2022.log
