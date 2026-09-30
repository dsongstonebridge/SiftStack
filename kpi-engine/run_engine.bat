@echo off
REM DAILY (6:30 PM task): fill the KPI Google Sheet from DataSift, then post the Slack summary
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ==== DAILY %date% %time% ==== >> engine_log.txt
py -3 fill_sheet.py >> engine_log.txt 2>&1
py -3 pull_kpis.py --days 1 --slack auto >> engine_log.txt 2>&1
