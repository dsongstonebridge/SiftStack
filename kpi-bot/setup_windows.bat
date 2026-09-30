@echo off
REM One-time: installs the bot libraries, then runs one test post to Slack
cd /d "%~dp0"
echo Installing Python libraries...
py -3 -m pip install -r requirements.txt
if errorlevel 1 (echo. & echo Python not found or install failed. Tell Claude what you see above. & pause & exit /b 1)
echo.
echo Running a test post to Slack...
call run_kpi_bot.bat
echo.
pause
