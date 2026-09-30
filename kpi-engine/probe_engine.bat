@echo off
REM DIAGNOSTIC: saves today's raw call records for Claude to read. Does NOT post to Slack.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Saving today's call details from DataSift (about a minute)...
echo ==== PROBE %date% %time% ==== >> engine_log.txt
py -3 pull_kpis.py --days 1 --probe >> engine_log.txt 2>&1
echo. & echo Done. Tell Claude "probe done".
pause
