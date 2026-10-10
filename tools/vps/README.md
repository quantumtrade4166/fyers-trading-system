# Rebuilding the VPS (runbook, 2026-10-11)

Everything lives under `C:\trading` (code: `C:\trading\fyers_data_pipeline`, ops: `C:\trading\ops`).
Owner rule: **backup first, everything backed up automatically.**

| Step | Who | What |
|------|-----|------|
| 1 | owner (RDP, admin PowerShell) | `tools\vps_step1_ssh.ps1` — SSH key-only, home IP only |
| 2 | owner | Install Google Drive for desktop, sign in (G:), sync Desktop/Documents/Downloads |
| 3 | Claude | `vps_step2_backup.ps1` — C:\trading root, IST clock, task VPSBackupToDrive (10 min + daily snapshots) |
| 4 | Claude | `vps_step3_base.ps1` — Python 3.12, Git, code zip (`git archive`), .venv + requirements, certifi fix |
| 5 | Claude | `vps_step4_restore.ps1` — state + .env from `G:\My Drive\VPS Backup` (overwrites; backup = truth) |
| 6 | Claude | copy `Nifty 500 Daily Data`, `Nifty 500 Daily Fyers`, `ETF data` from the PC; fix `.env` paths; `update_pair_data.py` with DATA_DIR set |
| 7 | Claude | `install_vps_tasks.ps1` — every scheduled task (DualMom disabled until brokers whitelist the IP) |
| 8 | owner click + Claude | `cloudflared tunnel login` (owner authorises within ~8 min), then `setup_tunnel.ps1` |
| 9 | owner | whitelist the VPS IP at Kotak (both UCCs) + Zerodha; re-arm strangles; then `install_vps_tasks.ps1 -EnableDualMom` |

Lessons: never deploy code by extracting over state files tracked in git; Windows Server needs the certifi fix;
run standalone scripts with the `.env` loaded (DATA_DIR etc.).
