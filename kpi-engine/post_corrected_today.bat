@echo off
REM Re-posts TODAY's numbers to Slack, marked as a correction.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Posting corrected numbers for today...
echo ==== CORRECTED %date% %time% ==== >> engine_log.txt
py -3 pull_kpis.py --days 1 --slack auto --note "CORRECTED: voicemails no longer counted as answered" >> engine_log.txt 2>&1
if errorlevel 1 (echo Something went wrong. Tell Claude.) else (echo Posted. Tell Claude "reposted".)
pause
