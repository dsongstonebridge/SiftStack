# Daily number-health post (Windows task "Number health post"), Jeff 2026-11-18.
#
# Fired at 9 AM and every 15 minutes until 7 PM, every day, plus at logon, on
# unlock and on wake. Each run is a seconds-long check; number_health_post.py
# decides whether anything is due (the 9 AM snapshot, or an update once today's
# NumberVerifier check lands for a number that was still on yesterday's).
$root = "C:\Users\dsong\OneDrive\Desktop\SiftStack"
$python = "C:\Users\dsong\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$now = Get-Date
if ($now.Hour -lt 9 -or $now.Hour -ge 19) { exit 0 }
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$log = Join-Path $root "logs\number_health_post.log"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $root
& $python "src\sms_agent\number_health_post.py" *>> $log
exit $LASTEXITCODE
