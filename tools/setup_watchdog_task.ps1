# tools/setup_watchdog_task.ps1 -- (re)register the DashboardWatchdog scheduled task (VPS).
# Runs the watchdog every 1 minute as SYSTEM. Idempotent (-Force). Run once after deploy.
# ASCII only (PowerShell 5.1).
$script = 'C:\Users\Administrator\Desktop\fyers_data_pipeline_git\tools\dashboard_watchdog.ps1'

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File $script"

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName 'DashboardWatchdog' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

$t = Get-ScheduledTask -TaskName 'DashboardWatchdog'
'registered: ' + $t.TaskName + ' / state=' + $t.State
'next run  : ' + (Get-ScheduledTaskInfo -TaskName 'DashboardWatchdog').NextRunTime
