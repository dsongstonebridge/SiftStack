@echo off
REM Schedules the KPI Engine Slack post for 6:30 PM Monday-Friday (run after a successful test)
schtasks /Create /TN "DataSift KPI Engine" /TR "\"%~dp0run_engine.bat\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /F
echo.
pause
