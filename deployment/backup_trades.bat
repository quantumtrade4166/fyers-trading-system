@echo off
echo ================================
echo  VPS Trade Data Backup
echo ================================
set BACKUP_DIR=G:\fyers_data_pipeline\deployment\backups
set VPS=Administrator@144.79.166.103
set VPS_DIR=C:/Users/Administrator/Desktop/fyers_data_pipeline_git/deployment

echo Backing up positions.json...
scp "%VPS%:%VPS_DIR%/positions.json" "%BACKUP_DIR%\positions.json"

echo Backing up trades.json...
scp "%VPS%:%VPS_DIR%/trades.json" "%BACKUP_DIR%\trades.json"

echo Backing up equity.json...
scp "%VPS%:%VPS_DIR%/equity.json" "%BACKUP_DIR%\equity.json"

echo.
echo Backup complete: %BACKUP_DIR%
echo Timestamp: %date% %time%
