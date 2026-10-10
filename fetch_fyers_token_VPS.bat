@echo off
echo ================================
echo  Fyers Token Sync (VPS -> Local)
echo ================================
echo.
echo Copying today's token from VPS to local...
scp Administrator@144.79.166.103:"C:/trading/fyers_data_pipeline/config/access_token.txt" "G:\fyers_data_pipeline\config\access_token.txt"
if %errorlevel%==0 (
    echo Token copied successfully. Local machine is ready.
) else (
    echo WARNING: Could not copy token from VPS. Check SSH connection.
)
pause
