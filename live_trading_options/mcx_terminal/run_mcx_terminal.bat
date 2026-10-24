@echo off
REM -- MCX Desk (MCX options terminal, Kotak P) launcher ----------------------------------
REM Run by the scheduled task "McxTerminal": at boot and every 5 minutes (a running engine makes the
REM extra start a no-op: the task ignores new instances and port 47680 admits one engine only).
REM The engine exits itself at 08:45 IST each new day; the next 5-minute start brings a fresh one
REM (new Kotak login, today's contract list, open paper positions carried over).
REM Served on 127.0.0.1:47680 -> Cloudflare tunnel -> https://mcx.trading.contact (Access + own lock).
REM PAPER: no code in this engine can send an order to Kotak.

cd /d C:\trading\fyers_data_pipeline
if not exist logs mkdir logs
.venv\Scripts\python.exe -u -m live_trading_options.mcx_terminal.server >> logs\mcx_terminal_stdout.log 2>&1
