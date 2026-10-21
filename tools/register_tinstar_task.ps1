# Registers (or re-registers) the Windows task for the weekly TinStar sheriff-sale check.
# Same trigger set as "KPI posts 630 PM weekdays", which was built after the PC slept
# through a scheduled run (2026-10-02): a timed trigger alone is not enough, because
# opening the lid is not a logon. No WakeToRun: it waits until the PC is in use.
#
#   powershell -ExecutionPolicy Bypass -File tools\register_tinstar_task.ps1
$ErrorActionPreference = "Stop"
$name = "TinStar sale check Mondays 9 AM"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$vbs  = Join-Path $repo "tools\run_hidden.vbs"
$ps1  = Join-Path $repo "tools\tinstar_check_task.ps1"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`" `"$ps1`""

$monday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At "9:00AM"
$logon  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$every  = New-ScheduledTaskTrigger -Daily -At "12:00AM"
$every.Repetition = (New-ScheduledTaskTrigger -Once -At "12:00AM" `
    -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
$unlockClass = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskSessionStateChangeTrigger
$unlock = New-CimInstance -CimClass $unlockClass -ClientOnly -Property @{ StateChange = 8; UserId = $env:USERNAME }  # 8 = unlock
$evtClass = Get-CimClass -Namespace Root/Microsoft/Windows/TaskScheduler -ClassName MSFT_TaskEventTrigger
$wake = New-CimInstance -CimClass $evtClass -ClientOnly -Property @{ Enabled = $true; Subscription =
    "<QueryList><Query Id=`"0`" Path=`"System`"><Select Path=`"System`">*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]</Select></Query></QueryList>" }

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Register-ScheduledTask -TaskName $name -Action $action -Trigger @($monday, $logon, $every, $unlock, $wake) `
    -Settings $settings -Principal $principal -Force | Out-Null
$t = Get-ScheduledTask -TaskName $name
"$name | state=$($t.State) | triggers=$($t.Triggers.Count) | next=$(($t | Get-ScheduledTaskInfo).NextRunTime)"
