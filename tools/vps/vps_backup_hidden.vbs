' Runs vps_backup.ps1 with no window and waits for it (so the task never overlaps itself).
Set s = CreateObject("WScript.Shell")
s.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\trading\ops\vps_backup.ps1""", 0, True
