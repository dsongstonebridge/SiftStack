@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ==== FILL-SHEET %date% %time% ==== >> engine_log.txt
py -3 fill_sheet.py --since 2026-09-28 >> engine_log.txt 2>&1
if errorlevel 1 (echo. & echo Something went wrong. Tell Claude "fill failed".) else (echo. & echo Done. Tell Claude "sheet filled".)
pause
