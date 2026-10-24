# install_practice_backfill.ps1 -- task PracticeBackfill (weekdays 16:30 IST) + start it once now.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
$act = New-ScheduledTaskAction -Execute "$repo\live_trading_options\strategy_builder\run_practice_backfill.bat" -WorkingDirectory $repo
$trg = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '16:30'
Register-ScheduledTask -TaskName 'PracticeBackfill' -Action $act -Trigger $trg -Principal $system -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName 'PracticeBackfill'
Start-Sleep 20
Get-ScheduledTask PracticeBackfill | Select-Object TaskName, State | Format-Table -AutoSize | Out-String
if (Test-Path "$repo\logs\practice_backfill.log") { Get-Content "$repo\logs\practice_backfill.log" -Tail 5 }
Remove-Item -Force $MyInvocation.MyCommand.Path
