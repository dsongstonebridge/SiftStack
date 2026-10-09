# Runs Diego's SMS agent for one weekday (Windows task "SMS agent weekdays").
# The task starts this at 8:00 AM and stops it around 7:00 PM. Sending only
# happens inside the agent's own hours; the agent itself enforces that.
# One log file per day. The redirect goes through cmd because Windows
# PowerShell 5.1 turns every stderr line (Python logging) into an error record.
$root = "C:\Users\dsong\OneDrive\Desktop\SiftStack"
$python = "C:\Users\dsong\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$log = Join-Path $root ("logs\sms_agent_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"
# Jeff, 2026-10-09: never two workers, never one left over from yesterday on
# old settings. The task's time limit ends this script but not the Python child,
# so stop any SMS worker still running before starting today's.
Get-CimInstance Win32_Process -Filter "name like 'python%'" |
    Where-Object { $_.CommandLine -like '*sms_agent*cli.py*work*' } |
    ForEach-Object {
        Add-Content -Path $log -Value ("{0} task: stopping leftover worker pid {1}" -f (Get-Date -Format s), $_.ProcessId)
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 3
cmd.exe /c "`"$python`" src\sms_agent\cli.py work --loop >> `"$log`" 2>&1"
exit $LASTEXITCODE
