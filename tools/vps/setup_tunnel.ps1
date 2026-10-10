# setup_tunnel.ps1 -- (re)creates the Cloudflare tunnel for the VPS web addresses.
#   dash.trading.contact   -> http://localhost:8000   (trading dashboard)
#   jarvis.trading.contact -> http://127.0.0.1:8020   (JARVIS; 502 until JARVIS is installed)
# Both stay behind Cloudflare Access (the Access apps are tied to the hostnames, not the tunnel).
#
# Needs C:\Users\Administrator\.cloudflared\cert.pem first: the owner authorises
# `cloudflared tunnel login` in a browser (one click). Then run this as Administrator.
# Config and credentials stay in .cloudflared, which tools\vps\vps_backup.ps1 backs up.
$ErrorActionPreference = 'Continue'
$repo = 'C:\trading\fyers_data_pipeline'
$exeDir = 'C:\Program Files (x86)\cloudflared'      # path JARVIS's installer expects
$cf = "$exeDir\cloudflared.exe"
$dir = 'C:\Users\Administrator\.cloudflared'

if (-not (Test-Path $cf)) {
    New-Item -ItemType Directory -Force $exeDir | Out-Null
    curl.exe -sL -o $cf https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
}
if (-not (Test-Path "$dir\cert.pem")) { 'cert.pem missing - run "cloudflared tunnel login" and authorise first'; exit 1 }

$name = 'vps-' + (Get-Date -Format 'yyyyMMdd')
$existing = & $cf tunnel list --output json 2>$null | ConvertFrom-Json | Where-Object { $_.name -eq $name -and (-not $_.deleted_at -or "$($_.deleted_at)" -like '0001*') }
if ($existing) { $id = $existing.id } else {
    & $cf tunnel create $name 2>&1 | ForEach-Object { "  | $_" }
    $id = (& $cf tunnel list --output json 2>$null | ConvertFrom-Json | Where-Object { $_.name -eq $name -and (-not $_.deleted_at -or "$($_.deleted_at)" -like '0001*') }).id
}
# use the tunnel whose credentials file is on this machine (a name can match more than one tunnel)
$id = @($id) | Where-Object { Test-Path "$dir\$_.json" } | Select-Object -First 1
if (-not $id) { 'tunnel create failed'; exit 1 }
"tunnel $name = $id"

@"
tunnel: $id
credentials-file: $dir\$id.json
ingress:
  - hostname: dash.trading.contact
    service: http://localhost:8000
  - hostname: jarvis.trading.contact
    service: http://127.0.0.1:8020
  - service: http_status:404
"@ | Set-Content "$dir\config.yml" -Encoding ascii

foreach ($h in 'dash.trading.contact', 'jarvis.trading.contact') {
    & $cf tunnel route dns --overwrite-dns $id $h 2>&1 | ForEach-Object { "  | $_" }
}

$a = New-ScheduledTaskAction -Execute "$repo\deployment\vps_tasks\start_cloudflared.bat" -WorkingDirectory $repo
$boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT30S'
$keep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'CloudflaredTunnel' -Action $a -Trigger @($boot, $keep) -Principal $p -Settings $s -Force | Out-Null
Get-Process cloudflared -EA SilentlyContinue | Stop-Process -Force
Start-ScheduledTask 'CloudflaredTunnel'
Unregister-ScheduledTask -TaskName 'CloudflaredLoginOnce' -Confirm:$false -EA SilentlyContinue
Start-Sleep 12
& $cf tunnel info $id 2>&1 | Select-Object -Last 4
