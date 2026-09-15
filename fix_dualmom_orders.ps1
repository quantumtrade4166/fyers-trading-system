# Fix for the 2026-09-15 DualMom Deploy: all 40 orders rejected ("error from core").
# Cause: every order reused the tag "dualmom"; Kotak treats the tag as the client
# order id and rejects repeats. This copies the unique-tag fix + safer month
# recording, voids the wrong "September done" record, restarts ONLY the DualMom
# service (dashboard + strangle untouched) and verifies.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$log  = Join-Path $root "logs\dualmom_fix_output.txt"
New-Item -ItemType Directory -Force -Path (Join-Path $root "logs") | Out-Null
Start-Transcript -Path $log -Force | Out-Null

$vps  = "Administrator@144.79.166.103"
$R    = "C:/Users/Administrator/Desktop/fyers_data_pipeline_git"

function Remote-PS([string]$script) {
    $script = "`$ProgressPreference = 'SilentlyContinue'`n" + $script
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    ssh -o BatchMode=yes $vps "powershell -NoProfile -EncodedCommand $enc"
}

Write-Host "`n1. Copying the fixed files..."
scp -o BatchMode=yes "$root\deployment\dualmom_live\kotak_equity.py" "$root\deployment\dualmom_live\config.py" "$root\deployment\dualmom_live\runner.py" "${vps}:$R/deployment/dualmom_live/"
$a = $LASTEXITCODE
scp -o BatchMode=yes "$root\deployment\dualmom_live_api.py" "$root\deployment\dualmom_service.py" "${vps}:$R/deployment/"
if ($a -ne 0 -or $LASTEXITCODE -ne 0) { Write-Host "COPY FAILED - nothing changed."; Stop-Transcript | Out-Null; exit 1 }

Write-Host "`n2. Voiding the wrong 'September done' record (0 of 40 filled)..."
Remote-PS @'
$st = "C:\Users\Administrator\Desktop\fyers_data_pipeline_git\deployment\dualmom_live_state"
$f  = Join-Path $st "month_gate.json"
if (Test-Path $f) { Move-Item $f (Join-Path $st "month_gate_VOID_20260915_all40_rejected.json") -Force; "voided month_gate.json" }
else { "no month_gate.json - nothing to void" }
'@

Write-Host "`n3. Restarting the DualMom service only..."
Remote-PS @'
$c = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
foreach ($x in $c) { Stop-Process -Id $x.OwningProcess -Force; "stopped pid $($x.OwningProcess)" }
'@

Write-Host "`n4. Verifying..."
$ok = $false
for ($i = 1; $i -le 12; $i++) {
    Start-Sleep -Seconds 10
    $s = Remote-PS @'
$R = "C:\Users\Administrator\Desktop\fyers_data_pipeline_git"
try { $st = (Invoke-WebRequest http://127.0.0.1:8010/api/dualmom/live/status -UseBasicParsing -TimeoutSec 60).Content } catch { $st = "not up yet" }
$gate = Test-Path "$R\deployment\dualmom_live_state\month_gate.json"
$tagfix = [bool](Select-String -Path "$R\deployment\dualmom_live\kotak_equity.py" -Pattern "def unique_tag" -Quiet)
"STATUS=$st"
"SEPT_RECORD_PRESENT=$gate"
"TAG_FIX_ON_DISK=$tagfix"
'@
    if ("$s" -match '"can_deploy":\s*true') {
        Write-Host "$s"
        $ok = ("$s" -match 'SEPT_RECORD_PRESENT=False') -and ("$s" -match 'TAG_FIX_ON_DISK=True') -and ("$s" -match '"order_tag":\s*"dm"')
        break
    }
    Write-Host "   ...waiting ($($i*10)s)"
}

if ($ok) { Write-Host "`n>>> FIXED. Go back to Claude and say ""fixed"" - then Preview again before Deploy." }
else     { Write-Host "`n>>> NOT CONFIRMED - do NOT Deploy. Go back to Claude and say ""not fixed""." }
Stop-Transcript | Out-Null
