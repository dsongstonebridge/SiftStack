# Checks both scheduled tasks, makes the sheet bot weekly (Fridays), and re-creates the KPI Engine
# task so it still runs if the PC was asleep/off at 6:30 (runs as soon as it wakes).
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$out  = Join-Path $here "schedule_status.txt"
"=== BEFORE $(Get-Date) ===" | Out-File $out -Encoding utf8
foreach ($n in "DataSift KPI Engine","DataSift KPI Bot") {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) { $i = $t | Get-ScheduledTaskInfo
    "$n | state=$($t.State) | last run=$($i.LastRunTime) | last result=$($i.LastTaskResult) | next run=$($i.NextRunTime)" | Out-File $out -Append -Encoding utf8
  } else { "$n | NOT FOUND" | Out-File $out -Append -Encoding utf8 }
}
# 1) Sheet bot: weekly instead of daily (Jeff, 2026-09-30) - Fridays 6:45 PM, after the engine
$botDir = Join-Path (Split-Path -Parent $here) "kpi-bot"
$botBat = Join-Path $botDir "run_kpi_bot.bat"
$ba = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$botBat`"" -WorkingDirectory $botDir
$bt = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At "6:45PM"
$bs = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "DataSift KPI Bot" -Action $ba -Trigger $bt -Settings $bs -Force | Out-Null
# 2) KPI Engine: weekdays 6:30 PM, catch up after sleep, wake the PC if it can
$bat = Join-Path $here "run_engine.bat"
$a = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $here
$tr = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "6:30PM"
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "DataSift KPI Engine" -Action $a -Trigger $tr -Settings $s -Force | Out-Null
"=== AFTER ===" | Out-File $out -Append -Encoding utf8
foreach ($n in "DataSift KPI Engine","DataSift KPI Bot") {
  $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
  if ($t) { $i = $t | Get-ScheduledTaskInfo
    "$n | state=$($t.State) | next run=$($i.NextRunTime)" | Out-File $out -Append -Encoding utf8
  } else { "$n | NOT FOUND" | Out-File $out -Append -Encoding utf8 }
}
