@echo off
REM DAILY: pulls today from DataSift and posts the labelled summary to Slack
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ==== DAILY %date% %time% ==== >> engine_log.txt
py -3 pull_kpis.py --days 1 --slack auto >> engine_log.txt 2>&1
