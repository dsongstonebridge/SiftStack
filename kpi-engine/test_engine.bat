@echo off
REM TEST: pulls the last 7 days from DataSift. Writes a report, does NOT post to Slack.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Installing helper libraries...
py -3 -m pip install --quiet tzdata openpyxl
echo Pulling the last 7 days from DataSift (can take a few minutes)...
echo ==== TEST %date% %time% ==== >> engine_log.txt
py -3 pull_kpis.py --days 7 --detail --xlsx >> engine_log.txt 2>&1
if errorlevel 1 (echo. & echo Something went wrong. Tell Claude "engine test done".) else (echo. & echo Done. Tell Claude "engine test done".)
pause
