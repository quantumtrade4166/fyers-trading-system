' Launches pc_backup.ps1 completely hidden (no PowerShell window).
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""G:\fyers_data_pipeline\tools\pc_backup.ps1""", 0, False
