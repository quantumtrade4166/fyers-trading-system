# vps_step2_backup.ps1 -- run on the VPS (Administrator). Creates the single root C:\trading
# and turns on the automatic backup to Google Drive BEFORE anything else is installed.
$ErrorActionPreference = 'Stop'
foreach ($p in 'C:\trading', 'C:\trading\ops', 'C:\trading\ops\logs', 'C:\trading\ops\tasks') { New-Item -ItemType Directory -Force -Path $p | Out-Null }
Copy-Item "$env:USERPROFILE\vps_setup\vps_backup.ps1", "$env:USERPROFILE\vps_setup\vps_backup_hidden.vbs" 'C:\trading\ops\' -Force

# Same clock as the old VPS and every IST schedule in the code.
Set-TimeZone -Id 'India Standard Time'

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '"C:\trading\ops\vps_backup_hidden.vbs"'
$t1 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$t2 = New-ScheduledTaskTrigger -AtLogOn -User 'Administrator'
$prin = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\Administrator" -LogonType Interactive -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'VPSBackupToDrive' -Action $action -Trigger @($t1, $t2) -Principal $prin -Settings $set -Force | Out-Null

# first run now, and wait for it
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\trading\ops\vps_backup.ps1'
"tz: " + (Get-TimeZone).Id + "  now: " + (Get-Date -Format 'yyyy-MM-dd HH:mm')
"task: " + (Get-ScheduledTask VPSBackupToDrive).State
Get-Content 'C:\trading\ops\vps_backup_status.json'
Get-ChildItem 'G:\My Drive\VPS Backup' | Select-Object Name, LastWriteTime | Format-Table -AutoSize | Out-String
