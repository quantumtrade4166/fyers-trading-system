"""Add the DualMom KITE record to the VPS Google-Drive backup (idempotent).

Inserts a mirror + dated daily archive of deployment/dualmom_kite_state right
before the CONFIG/SECRETS block of Desktop/vps_backup_to_drive.ps1, exactly like
the Kotak block. Keeps a .bak copy. Does nothing if already present."""
import shutil
import sys
from pathlib import Path

p = Path(r"C:\Users\Administrator\Desktop\vps_backup_to_drive.ps1")
s = p.read_text(encoding="utf-8-sig")
if "dualmom_kite_state" in s:
    print("backup: Kite already included")
    sys.exit(0)
anchor = "# ---- CONFIG / SECRETS"
if anchor not in s:
    print("backup: anchor not found - NOT patched (tell Claude)")
    sys.exit(1)
block = r'''# ---- DUALMOM KITE (main Zerodha, DualMom own book) -- added 2026-09-21 ----
robocopy "$repo\deployment\dualmom_kite_state" "$td\dualmom_kite_state" /E @rc @xd | Out-Null
$arck = "$td\dualmom_kite_ledger_archive\$day"
if ((Get-Date).Hour -ge 16 -and (Test-Path "$repo\deployment\dualmom_kite_state\ledger") -and -not (Test-Path $arck)) {
  robocopy "$repo\deployment\dualmom_kite_state\ledger" $arck /E @rc | Out-Null
}

'''
shutil.copy2(p, p.with_name(p.name + ".bak-20260921"))
bom = p.read_bytes().startswith(b"\xef\xbb\xbf")      # PowerShell 5.1 needs it kept
p.write_text(s.replace(anchor, block + anchor, 1), encoding="utf-8-sig" if bom else "utf-8")
print("backup: Kite record added to the Drive backup")
