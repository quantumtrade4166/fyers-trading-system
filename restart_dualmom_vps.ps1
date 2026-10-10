# Restart only the DualMom service on the VPS.
# Strangle/DN/dashboard all run in other processes - they are NOT touched.
$ErrorActionPreference = "Continue"
$vps = "Administrator@103.49.131.58"

$script = @'
$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*deployment.dualmom_service*' }
foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 3
schtasks /Run /TN DualMomService | Out-Null
Write-Host 'service_restarted'
'@

$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
ssh -o BatchMode=yes -o ConnectTimeout=20 $vps "powershell -NoProfile -EncodedCommand $enc"
