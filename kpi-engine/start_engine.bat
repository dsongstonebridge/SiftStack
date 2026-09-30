@echo off
REM One-time: posts today's numbers to Slack now, then schedules the 6:30 PM weekday post.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Pulling today from DataSift and posting to Slack (takes a minute)...
echo ==== START %date% %time% ==== >> engine_log.txt
py -3 pull_kpis.py --days 1 --slack auto >> engine_log.txt 2>&1
if errorlevel 1 (echo. & echo Something went wrong - nothing was scheduled. Tell Claude "engine failed". & pause & exit /b 1)
echo Posted. Now scheduling the 6:30 PM weekday post...
schtasks /Create /TN "DataSift KPI Engine" /TR "\"%~dp0run_engine.bat\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /F
echo.
echo All set. Tell Claude "engine started".
pause
