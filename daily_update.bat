@echo off
echo ================================
echo  Nifty FnO Daily Data Update
echo ================================
cd /d G:\fyers_data_pipeline
python run_pipeline.py --mode update
echo.
echo Update complete!
pause