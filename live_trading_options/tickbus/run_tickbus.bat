@echo off
REM Tick bus launcher: ONE Kite websocket shared by the MCX / NSE desks, their order gateways and the
REM    CE/PE + ALGO engines (feedlink/kite_assist.py). 25-Nov-2026.
REM Task "TickBus" (SYSTEM) 08:55 Mon-Fri. run_bus.py exits non-zero until today's Kite token exists
REM (Zerodha auto-login 09:00) and ends itself at 23:50 IST; this loop restarts it after any exit and
REM stops after 23:50. Consumers keep running on Kotak alone while the bus is down.
cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
:loop
for /f %%h in ('powershell -NoProfile -Command "(Get-Date).ToString('HHmm')"') do set HM=%%h
if %HM% GEQ 2350 goto :eof
.venv\Scripts\python.exe -u live_trading_options\tickbus\run_bus.py >> logs\tickbus.log 2>&1
ping -n 16 127.0.0.1 >nul
goto loop
