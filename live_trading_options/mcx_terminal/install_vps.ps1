# install_vps.ps1 -- MCX Desk on the VPS: scheduled task + tunnel hostname. Run as Administrator, from anywhere:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\trading\fyers_data_pipeline\live_trading_options\mcx_terminal\install_vps.ps1
#
# 1. Task "McxTerminal" (SYSTEM): boot + every 5 min, restart on failure, one instance.
#    Any engine started by hand (SSH) is stopped first, by PID, so the task owns the port.
# 2. Tunnel: adds  mcx.trading.contact -> http://127.0.0.1:47680  to .cloudflared\config.yml (other
#    hostnames untouched), routes the DNS name, restarts cloudflared (dash/jarvis blip a few seconds).
#    Safe before Cloudflare Access exists: the engine refuses every external request until
#    MCX_ACCESS_TEAM + MCX_ACCESS_AUD are in deployment\.env (see access.py).
$ErrorActionPreference = 'Continue'
$repo = 'C:\trading\fyers_data_pipeline'
$bat = "$repo\live_trading_options\mcx_terminal\run_mcx_terminal.bat"
$host_ = 'mcx.trading.contact'
$cfdir = 'C:\Users\Administrator\.cloudflared'
$cf = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'

# ── 1. task ──
$a = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $repo
$boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT40S'
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
     -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'McxTerminal' -Action $a -Trigger @($boot, $every) -Principal $p -Settings $s -Force | Out-Null
'task: McxTerminal registered'

Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*mcx_terminal.server*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "stopped hand-started engine PID $($_.ProcessId)" }
Start-Sleep 2
Start-ScheduledTask -TaskName 'McxTerminal'
$ok = $false
foreach ($i in 1..30) {
    Start-Sleep 2
    try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:47680/health' -TimeoutSec 3; if ($h.feed.authed) { $ok = $true; break } } catch {}
}
if ($ok) { "engine up: feed authed, subscribed $($h.feed.subscribed), mode $($h.mode)" } else { 'WARNING: engine not healthy after 60 s - see logs\mcx_terminal.log' }

# ── 2. tunnel hostname ──
$cfg = Get-Content "$cfdir\config.yml" -Raw
if ($cfg -notmatch [regex]::Escape($host_)) {
    Copy-Item "$cfdir\config.yml" "$cfdir\config.yml.bak_$(Get-Date -Format yyyyMMdd_HHmmss)"
    $cfg = $cfg -replace '(\s*- service: http_status:404)', "`r`n  - hostname: $host_`r`n    service: http://127.0.0.1:47680`$1"
    Set-Content "$cfdir\config.yml" $cfg -Encoding ascii -NoNewline
    "tunnel: added $host_ (backup kept)"
} else { "tunnel: $host_ already in config.yml" }
$id = ([regex]::Match($cfg, 'tunnel:\s*([0-9a-f-]{36})')).Groups[1].Value
& $cf tunnel ingress validate --config "$cfdir\config.yml" 2>&1 | Select-Object -Last 1
& $cf tunnel route dns $id $host_ 2>&1 | ForEach-Object { "  dns | $_" }
Get-Process cloudflared -EA SilentlyContinue | Stop-Process -Force
Start-ScheduledTask 'CloudflaredTunnel'
Start-Sleep 10
"cloudflared running: " + [bool](Get-Process cloudflared -EA SilentlyContinue)
