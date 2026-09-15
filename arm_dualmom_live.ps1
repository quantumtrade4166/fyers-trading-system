# Switch DualMom LIVE on the VPS (Kotak Rohit, UCC 15P56).
# Copies the armed config + the tag fix, restarts ONLY the DualMom service
# (the dashboard and the strangle are not touched), then shows the live status.
#
# v2: the first version sent the restart as a quoted PowerShell string. Windows
# PowerShell 5.1 strips embedded double quotes when calling ssh, so the VPS ran it
# through cmd, split it at the "|" and failed ("'ForEach-Object' is not
# recognized") - files were copied but the service never restarted. The restart is
# now sent -EncodedCommand (base64), which contains no quotes or pipes at all.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root "logs\dualmom_arm_output.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Start-Transcript -Path $log -Force | Out-Null

$vps  = "Administrator@144.79.166.103"
$dest = "C:/Users/Administrator/Desktop/fyers_data_pipeline_git/deployment/dualmom_live/"

function Remote-PS([string]$script) {
    $script = "`$ProgressPreference = 'SilentlyContinue'`n" + $script
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    ssh -o BatchMode=yes $vps "powershell -NoProfile -EncodedCommand $enc"
}

Write-Host "`n1. Copying config.py (live ON) and kotak_equity.py (tag fix) to the VPS..."
scp -o BatchMode=yes "$root\deployment\dualmom_live\config.py" "$root\deployment\dualmom_live\kotak_equity.py" "${vps}:$dest"
if ($LASTEXITCODE -ne 0) { Write-Host "COPY FAILED - nothing was switched on."; Stop-Transcript | Out-Null; exit 1 }

Write-Host "`n2. Restarting the DualMom service only..."
Remote-PS @'
$c = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
if ($c) { foreach ($x in $c) { Stop-Process -Id $x.OwningProcess -Force; "stopped DualMom service pid $($x.OwningProcess) - supervisor relaunches it in 15s" } }
else { "DualMom service was not listening on 8010 - supervisor should start it" }
'@

Write-Host "`n3. Waiting for it to come back..."
$armed = $false
for ($i = 1; $i -le 12; $i++) {
    Start-Sleep -Seconds 10
    $s = Remote-PS @'
try { (Invoke-WebRequest http://127.0.0.1:8010/api/dualmom/live/status -UseBasicParsing -TimeoutSec 60).Content } catch { "not up yet" }
'@
    if ("$s" -match '"can_deploy":\s*true') { $armed = $true; Write-Host "`n4. Live status:`n$s"; break }
    if ("$s" -match '"can_deploy":\s*false') { Write-Host "`n4. Live status:`n$s"; break }
    Write-Host "   ...waiting ($($i*10)s)"
}

if ($armed) { Write-Host "`n>>> ARMED: can_deploy is TRUE. Go back to Claude and say ""armed""." }
else        { Write-Host "`n>>> NOT ARMED - do NOT click Deploy. Go back to Claude and say ""not armed""." }
Stop-Transcript | Out-Null
