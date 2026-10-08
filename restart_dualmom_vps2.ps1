$script = @'
$p = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -match 'deployment\.dualmom_service') }
foreach ($x in $p) { Stop-Process -Id $x.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
schtasks /Run /TN DualMomService
'@
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
ssh -o BatchMode=yes -o ConnectTimeout=20 Administrator@144.79.166.103 "powershell -NoProfile -EncodedCommand $enc"
