@echo off
REM Runs the DataSift KPI bot once and logs the result to run_log.txt
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ==== %date% %time% ==== >> run_log.txt
py -3 kpi_slack_bot.py >> run_log.txt 2>&1
if errorlevel 1 (echo FAILED - see run_log.txt) else (echo KPI update posted to Slack.)
