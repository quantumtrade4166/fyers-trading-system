# Registers the scheduled task "BtcTickRecorder" (SYSTEM, at boot + every 5 min, never two at once).
# Run once on the VPS from an elevated PowerShell:  powershell -File install_task.ps1   (-Status / -Remove)
param([switch]$Status, [switch]$Remove)
$name = 'BtcTickRecorder'
if ($Status) { Get-ScheduledTask -TaskName $name | Select-Object TaskName, State; Get-ScheduledTaskInfo -TaskName $name | Select-Object LastRunTime, LastTaskResult, NextRunTime; exit }
if ($Remove) { Unregister-ScheduledTask -TaskName $name -Confirm:$false; exit }
$bat = 'C:\trading\fyers_data_pipeline\live_trading_options\btc_tick_recorder\run_btc_ticks.bat'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$bat`"" -WorkingDirectory 'C:\trading\fyers_data_pipeline'
$boot = New-ScheduledTaskTrigger -AtStartup
$rep = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$pr = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $name -Action $action -Trigger @($boot, $rep) -Settings $set -Principal $pr -Force | Out-Null
Start-ScheduledTask -TaskName $name
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
