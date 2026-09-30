@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_schedule.ps1"
type schedule_status.txt
echo.
echo Posting Tuesday's missed numbers to Slack...
echo ==== MISSED-TUESDAY %date% %time% ==== >> engine_log.txt
py -3 pull_kpis.py --from 2026-09-29 --to 2026-09-29 --slack auto --note "Tuesday 9/29 (missed post)" >> engine_log.txt 2>&1
echo.
echo Done. Tell Claude "schedule fixed".
pause
