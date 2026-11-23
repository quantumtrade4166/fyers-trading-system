@echo off
REM Synthetic replay player: starts a local web server for this folder (if not already running) and opens the page.
cd /d G:\fyers_data_pipeline\replays
powershell -NoProfile -Command "try { (Invoke-WebRequest http://127.0.0.1:8779/syn_player.html -UseBasicParsing -TimeoutSec 2) | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
    start "Replay server (close to stop)" /min G:\fyers_data_pipeline\.venv\Scripts\python.exe -m http.server 8779 --bind 127.0.0.1 --directory G:\fyers_data_pipeline\replays
    ping -n 3 127.0.0.1 >nul
)
start "" http://127.0.0.1:8779/syn_player.html
