@echo off
REM READ-ONLY survey of DataSift, smrtPhone and the KPI sheet. Changes nothing.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Surveying DataSift, smrtPhone and your KPI sheet (read-only, a few minutes)...
echo ==== DISCOVER %date% %time% ==== >> engine_log.txt
py -3 discover.py 30 >> engine_log.txt 2>&1
echo. & echo Done. Tell Claude "discovery done".
pause
