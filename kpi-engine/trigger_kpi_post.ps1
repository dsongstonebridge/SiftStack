# Presses "Run workflow" on the GitHub "KPI posts" workflow for each weekday whose 6:30 PM
# has passed and has not been posted yet. Run by the Windows task "KPI posts - 6:30 PM".
# Jeff, 2026-10-02: GitHub's own timer ran hours late; this fires on time, and if the PC was
# asleep or off it catches up as soon as it is back, posting the day that was missed (not today).
# Fridays post "both" (daily + weekly sheet). State: reports\kpi_post_last.txt (last day posted).

$ErrorActionPreference = "Stop"
$gh    = "C:\Program Files\GitHub CLI\gh.exe"
$repo  = "dsongstonebridge/SiftStack"
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $here "reports\kpi_post_last.txt"
$log   = Join-Path $here "reports\kpi_post_trigger.log"
New-Item -ItemType Directory -Force (Split-Path $state) | Out-Null

function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Out-File -Append -Encoding utf8 $log }

$now  = Get-Date
$last = if (Test-Path $state) { [datetime]::ParseExact((Get-Content $state -Raw).Trim(), "yyyy-MM-dd", $null) }
        else { $now.Date.AddDays(-1) }

# every weekday after the last posted day whose 6:30 PM is already in the past (max 7 back)
$due = @()
$d = $last.AddDays(1)
if ($d -lt $now.Date.AddDays(-7)) { $d = $now.Date.AddDays(-7) }
while ($d -le $now.Date) {
    if ($d.DayOfWeek -ne "Saturday" -and $d.DayOfWeek -ne "Sunday" -and $now -ge $d.AddHours(18).AddMinutes(30)) {
        $due += $d
    }
    $d = $d.AddDays(1)
}
if (-not $due) { Log "nothing due (last posted $($last.ToString('yyyy-MM-dd')))"; exit 0 }

foreach ($day in $due) {
    $what = if ($day.DayOfWeek -eq "Friday") { "both" } else { "daily" }
    $date = $day.ToString("yyyy-MM-dd")
    $ok = $false
    # right after waking, the network may not be up yet: retry for up to ~15 minutes
    for ($try = 1; $try -le 15 -and -not $ok; $try++) {
        & $gh workflow run kpi-posts.yml --repo $repo -f what=$what -f date=$date 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $ok = $true } else { Start-Sleep -Seconds 60 }
    }
    if (-not $ok) { Log "FAILED to start $what for $date after 15 tries"; exit 1 }
    $date | Out-File -Encoding ascii -NoNewline $state
    Log "started $what for $date$(if ($day.Date -ne $now.Date) { ' (catch-up)' })"
    if ($due.Count -gt 1) { Start-Sleep -Seconds 90 }   # one run at a time
}
exit 0
