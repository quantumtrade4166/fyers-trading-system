@echo off
REM ============================================================================
REM  BTC delta-neutral strangle — all three session profiles, PAPER ONLY.
REM
REM  A  ist_day      09:30 -> 17:10 IST, same-day expiry
REM  B  full_cycle   17:35 -> 17:10 next day, one strangle per expiry
REM  C  continuous   17:35 -> 17:10 next day, re-enters after a stop-out
REM
REM  No API key is required and no order can reach an exchange — the executor's
REM  live path raises rather than being merely switched off.
REM
REM  Paths are derived from THIS FILE's location so the same script works on the
REM  local G: drive and on the VPS under fyers_data_pipeline_git.
REM
REM  Pure stdlib — no pandas, no pyarrow, no broker SDK. That is deliberate: the
REM  fewer things in the runtime path, the fewer ways a month-long run dies.
REM
REM  The engine exits on a feed stall; the scheduled task restarts it and it
REM  resumes any cycle in flight. Safe to double-launch (loopback lock, 47655).
REM ============================================================================
setlocal
set "REPO=%~dp0..\.."
cd /d "%REPO%"
"%REPO%\.venv\Scripts\python.exe" "%~dp0engine.py"
