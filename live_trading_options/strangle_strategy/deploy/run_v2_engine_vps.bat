@echo off
REM V2 tick engine (Vwap Strangle) — STANDALONE, paper only, own WebSocket.
REM Start during market hours AFTER 9:20 (needs the day's strike selection).
REM Writes {date}_{index}_V2.json for V1-vs-V2 comparison. Self-exits after 15:35.
REM SUPERVISED first run: after starting, confirm the StatArb live feed is still
REM alive (single-socket-per-token risk is untested).
cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
.venv\Scripts\python.exe live_trading_options\strangle_strategy\live_tick_engine.py >> logs\strangle_v2.log 2>&1
