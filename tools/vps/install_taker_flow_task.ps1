# install_taker_flow_task.ps1 -- the TakerFlowEngine task only (Taker Flow L/S, PAPER).
# Run on the VPS as Administrator:
#     powershell -ExecutionPolicy Bypass -File C:\trading\fyers_data_pipeline\tools\vps\install_taker_flow_task.ps1
# Same definition as the line in install_vps_tasks.ps1: SYSTEM, boot + every 5 min
# (watchdog; the engine's lock port refuses a second copy), no time limit, restart on failure.
$ErrorActionPreference = 'Stop'
$repo = 'C:\trading\fyers_data_pipeline'
$system = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
$boot = New-ScheduledTaskTrigger -AtStartup; $boot.Delay = 'PT30S'
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
$act = New-ScheduledTaskAction -Execute "$repo\live_trading_options\taker_flow\run_taker_flow_engine.bat" -WorkingDirectory $repo
Register-ScheduledTask -TaskName 'TakerFlowEngine' -Action $act -Trigger @($boot, $every) -Principal $system -Settings $set -Force | Out-Null
Start-ScheduledTask -TaskName 'TakerFlowEngine'
"task: TakerFlowEngine (started)"
