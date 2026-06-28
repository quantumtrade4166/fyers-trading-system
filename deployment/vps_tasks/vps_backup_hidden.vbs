' Hidden launcher for vps_backup_to_drive.ps1 (no PowerShell window).
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\Administrator\Desktop\vps_backup_to_drive.ps1""", 0, False
