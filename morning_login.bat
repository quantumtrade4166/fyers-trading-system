@echo off
echo ================================
echo  Fyers Morning Login
echo ================================
cd /d G:\fyers_data_pipeline
python auth\fyers_auth.py

echo.
echo Copying access token to VPS via scp...
scp "G:\fyers_data_pipeline\config\access_token.txt" Administrator@144.79.166.103:"C:/Users/Administrator/Desktop/fyers_data_pipeline_git/config/access_token.txt"
if %errorlevel%==0 (
    echo Token copied to VPS successfully.
) else (
    echo WARNING: Could not copy token to VPS via scp. Check SSH key setup.
)
pause
