@echo off
REM KPI posts now run on GitHub. This pauses the PC copies so nothing posts twice.
REM To turn them back on later: schtasks /Change /TN "DataSift KPI Engine" /ENABLE  (same for "DataSift KPI Bot")
schtasks /Change /TN "DataSift KPI Engine" /DISABLE
schtasks /Change /TN "DataSift KPI Bot" /DISABLE
echo.
echo Done. The PC schedule is paused; GitHub handles the posts now. Tell Claude "pc paused".
pause
