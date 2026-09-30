@echo off
REM One-time: log in to smrtPhone so the KPI reports can read call logs and dialer sessions.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo Installing the browser tool (first time only, a few minutes)...
py -3 -m pip install --quiet playwright
py -3 -m playwright install chromium
echo.
echo A browser window will open - log in to smrtPhone there.
py -3 smrtphone_login.py
pause
