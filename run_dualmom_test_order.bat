@echo off
REM DualMom go-live step 6: ONE real 1-share IDEA buy on Kotak Rohit (UCC 15P56).
REM Runs on the VPS over SSH. Output is saved so it can be checked afterwards.
echo.
echo  Placing the 1-share IDEA test order on Kotak Rohit (DualMom)...
echo  This takes up to 2-3 minutes. Do not close this window.
echo.
if not exist "%~dp0logs" mkdir "%~dp0logs"
ssh -o BatchMode=yes Administrator@144.79.166.103 "cd /d C:\Users\Administrator\Desktop\fyers_data_pipeline_git && .venv\Scripts\python.exe -m deployment.dualmom_live.first_order_test --yes" > "%~dp0logs\dualmom_step6_output.txt" 2>&1
type "%~dp0logs\dualmom_step6_output.txt"
echo.
echo  Done. Output saved to logs\dualmom_step6_output.txt
echo  Go back to Claude and say "done".
pause
