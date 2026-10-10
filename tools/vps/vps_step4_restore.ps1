# vps_step4_restore.ps1 -- latest code + trade data/config restored from the Drive backup.
# /XO: a file already on the VPS that is newer is never overwritten by an older backup.
$ErrorActionPreference = 'Continue'
$repo = 'C:\trading\fyers_data_pipeline'
$src  = 'G:\My Drive\VPS Backup'
$log  = 'C:\trading\ops\logs\setup.log'
function Log($m) { $l = "{0}  {1}" -f (Get-Date -Format 'HH:mm:ss'), $m; $l; Add-Content $log $l }

Expand-Archive "$env:USERPROFILE\vps_setup\code.zip" $repo -Force
Log 'code refreshed to latest commit'

$rc = @('/R:2', '/W:5', '/NFL', '/NDL', '/NP', '/NJH', '/NJS')   # no /XO: backup = truth for state (git placeholders are newer but blank)
$map = @(
  @("$src\Trade Data\deployment",         "$repo\deployment",                                          $false),
  @("$src\Trade Data\dualmom_live_state", "$repo\deployment\dualmom_live_state",                       $true),
  @("$src\Trade Data\dualmom_kite_state", "$repo\deployment\dualmom_kite_state",                       $true),
  @("$src\Trade Data\strangle_flags",     "$repo\strangle_system\flags",                               $true),
  @("$src\Trade Data\chart_history",      "$repo\live_trading_options\strangle_strategy\data\chart_history", $true)
)
foreach ($m in $map) {
  $a = @($m[0], $m[1]) + $rc; if ($m[2]) { $a += '/E' }
  & robocopy @a | Out-Null
  Log ("restored {0} -> {1} rc={2} files now={3}" -f (Split-Path $m[0] -Leaf), $m[1].Replace($repo,''), $LASTEXITCODE, (Get-ChildItem $m[1] -Recurse -File -EA SilentlyContinue).Count)
}
& robocopy "$src\Config" "$repo\deployment" .env @rc | Out-Null
& robocopy "$src\Config" "$repo\config" access_token.txt settings.py symbols.py @rc | Out-Null
Log (".env present: " + (Test-Path "$repo\deployment\.env") + "  config files: " + ((Get-ChildItem "$repo\config" -File).Name -join ','))
Log 'step4 done'
