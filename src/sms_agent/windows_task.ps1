# Keeps Diego's SMS agent running on weekdays (Windows task "SMS agent weekdays").
#
# Jeff, 2026-10-29: the agent must be up at 8:00 AM, or as soon as the PC is
# first awake/used after that, EVERY weekday, unless he says not to (pause =
# disable the task). A single 8 AM start was not enough: on 10/29 the PC woke
# at 8:34, yesterday's worker (asleep since 6:50 PM) stopped itself as it
# should, and the one daily run was used up with no worker started.
#
# So this script is a quick, repeatable CHECK, not the worker's parent. The
# task fires it at 8 AM and every 15 minutes until 7 PM, at logon, on unlock
# and on wake. Each time:
#   - weekend, before 8 AM, or 7 PM and later: do nothing
#   - a worker started today is running: do nothing
#   - a worker from an earlier day is running: stop it (never run on
#     yesterday's settings, Jeff 2026-10-09), then start today's
#   - no worker: start one, detached, so this script exits in seconds
# The worker still stops itself at 7 PM, and instance_lock.py still refuses a
# second worker, so a double start cannot double send.
$root = "C:\Users\dsong\OneDrive\Desktop\SiftStack"
$python = "C:\Users\dsong\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$now = Get-Date
$log = Join-Path $root ("logs\sms_agent_{0}.log" -f $now.ToString("yyyy-MM-dd"))
# The worker holds $log open, so this script notes what it did in its own file.
$tasklog = Join-Path $root "logs\sms_agent_task.log"
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null

if ($now.DayOfWeek -in 'Saturday', 'Sunday') { exit 0 }
if ($now.Hour -lt 8 -or $now.Hour -ge 19) { exit 0 }

$workers = @(Get-CimInstance Win32_Process -Filter "name like 'python%'" |
    Where-Object { $_.CommandLine -like '*sms_agent*cli.py*work*' })
$today = @($workers | Where-Object { $_.CreationDate.Date -eq $now.Date })
if ($today.Count -gt 0) { exit 0 }

foreach ($w in $workers) {
    Add-Content -Path $tasklog -Value ("{0} task: stopping leftover worker pid {1} from {2}" -f (Get-Date -Format s), $w.ProcessId, $w.CreationDate)
    Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($workers.Count -gt 0) { Start-Sleep -Seconds 3 }

# Started through WMI so the worker is not inside the task's job object: the
# task ends as soon as this script does, and the worker keeps running.
# A WMI-created process does not inherit this script's environment, so the
# encoding is set inside the command line.
$cmd = "cmd.exe /c `"set PYTHONIOENCODING=utf-8&& `"$python`" src\sms_agent\cli.py work --loop >> `"$log`" 2>&1`""
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $cmd; CurrentDirectory = $root; ProcessStartupInformation = $startup }
Add-Content -Path $tasklog -Value ("{0} task: started worker (rc {1}, pid {2})" -f (Get-Date -Format s), $r.ReturnValue, $r.ProcessId)
exit $r.ReturnValue
