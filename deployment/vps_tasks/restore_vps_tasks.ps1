# restore_vps_tasks.ps1  --  Recreate all VPS scheduled tasks + launchers from the
# exported definitions in this folder. Run ON THE VPS as Administrator after the repo
# is cloned and the venv is built. See vault note "VPS Recovery" for the full procedure.
#
#   powershell -ExecutionPolicy Bypass -File restore_vps_tasks.ps1
#
# Idempotent: /F overwrites an existing task of the same name.

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$desktop = "C:\Users\Administrator\Desktop"

# 1. Put the launcher .bat files back on the Desktop (tasks reference these paths)
foreach ($b in 'start_dashboard.bat','start_cloudflared.bat','fyers_auto_login.bat') {
    $src = Join-Path $here $b
    if (Test-Path $src) { Copy-Item $src (Join-Path $desktop $b) -Force; "placed $b" }
}

# 2. Recreate every scheduled task from its exported XML (runs as SYSTEM per the XML)
Get-ChildItem (Join-Path $here '*.xml') | ForEach-Object {
    $name = $_.BaseName
    schtasks /create /tn $name /xml $_.FullName /f | Out-Null
    "task restored: $name"
}

Write-Output "`nDone. Verify with:  Get-ScheduledTask | ? { `$_.TaskPath -eq '\' }"
Write-Output "Then start the services:  Start-ScheduledTask -TaskName PairsDashboard ; Start-ScheduledTask -TaskName CloudflaredTunnel"
