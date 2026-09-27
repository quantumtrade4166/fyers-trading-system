@echo off
REM start_jarvis_tunnel.bat — Cloudflare Tunnel for JARVIS API (port 8081)
REM
REM Separate from the dashboard tunnel. Gives JARVIS its own public URL.
REM Run alongside start_cloudflared.bat.

cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git
echo [%date% %time%] Starting JARVIS API tunnel... >> logs\jarvis_api.log
cloudflared tunnel --url http://localhost:8081 >> logs\jarvis_api.log 2>&1
