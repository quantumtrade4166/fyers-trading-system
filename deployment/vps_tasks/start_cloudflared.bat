@echo off
rem Named Cloudflare tunnel: dash.trading.contact -> :8000, jarvis.trading.contact -> :8020.
rem Config + credentials live in C:\Users\Administrator\.cloudflared (backed up by tools\vps\vps_backup.ps1).
rem Recreate with tools\vps\setup_tunnel.ps1. Task CloudflaredTunnel (boot + 5-min keep-alive).
"C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --no-autoupdate --config "C:\Users\Administrator\.cloudflared\config.yml" run
