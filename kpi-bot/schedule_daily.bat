@echo off
REM Schedules the KPI bot for 6:30 PM Monday-Friday (run only after a successful test)
schtasks /Create /TN "DataSift KPI Bot" /TR "\"%~dp0run_kpi_bot.bat\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /F
echo.
pause
